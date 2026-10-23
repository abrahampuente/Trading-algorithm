"""
Servicio de ejecución (Hito 3): une broker, planificador y persistencia.

- Despacha planes de órdenes (entradas `OPG`, salidas OCO, salidas
  temporales) y guarda cada orden en `broker_orders` con su momento local de
  envío.
- Sincroniza estados del broker (`apply_state`), que deduce y registra las
  ejecuciones en `order_executions`, y cancela órdenes.
- Mide tiempos: duración de la llamada de envío, latencia de cada petición
  HTTP (si el broker la registra) y tiempo hasta un estado del broker
  (p. ej. aceptada o ejecutada) por sondeo.

El servicio confirma la transacción tras cada operación, de modo que MySQL
refleja siempre lo que ya se envió al broker; ante un error deshace la
transacción en curso y lo propaga.

Envío idempotente: si `client_order_id` ya está guardado, no se reenvía.
"""

import time
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from algo_trading.execution.alpaca_broker import RequestTiming
from algo_trading.execution.broker import BrokerAdapter
from algo_trading.execution.order_planner import OrderPlanner
from algo_trading.execution.orders import OrderRequest, OrderState
from algo_trading.persistence.repositories.broker_order_repository import (
    BrokerOrderRepository,
    OrderSyncResult,
    UnknownOrderError,
)
from algo_trading.portfolio.allocator import PlannedEntry

# Estados de Alpaca en los que el broker ya aceptó la orden (`pending_new`
# todavía no).
ACCEPTED_BROKER_STATUSES = frozenset(
    {"accepted", "new", "partially_filled", "filled", "done_for_day"}
)


@runtime_checkable
class RequestLoggingBroker(Protocol):
    @property
    def request_log(self) -> tuple[RequestTiming, ...]: ...


class SubmissionTiming(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_sent_at: datetime
    response_received_at: datetime
    submit_call_ms: float = Field(ge=0)
    http_requests: tuple[RequestTiming, ...] = ()

    @property
    def order_request_latency_ms(self) -> float | None:
        """Latencia del último `POST /v2/orders` (red + API)."""
        posts = [
            request
            for request in self.http_requests
            if request.method == "POST" and request.status is not None
        ]
        return posts[-1].latency_ms if posts else None


class DispatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request: OrderRequest
    state: OrderState
    reused: bool
    sync: OrderSyncResult | None = None
    timing: SubmissionTiming | None = None


class StatusWaitResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: OrderState
    reached: bool
    polls: int = Field(ge=1)
    elapsed_ms: float = Field(ge=0)
    transitions: tuple[OrderSyncResult, ...]


class ExecutionService:
    def __init__(
        self,
        broker: BrokerAdapter,
        session: Session,
        planner: OrderPlanner | None = None,
        repository: BrokerOrderRepository | None = None,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._broker = broker
        self._session = session
        self._planner = planner or OrderPlanner()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._repository = repository or BrokerOrderRepository(session, self._clock)
        self._timer = timer or time.perf_counter
        self._sleep = sleep or time.sleep

    def submit(self, request: OrderRequest) -> DispatchResult:
        existing = self._repository.get(request.client_order_id)

        if existing is not None:
            return DispatchResult(request=request, state=existing, reused=True)

        log_start = self._request_log_length()
        sent_at = self._clock()
        started = self._timer()
        state = self._broker.submit_order(request)
        elapsed_ms = (self._timer() - started) * 1000
        received_at = self._clock()

        sync = self._transaction(
            lambda: self._repository.record_submission(
                request, state, created_at=sent_at
            )
        )
        return DispatchResult(
            request=request,
            state=state,
            reused=False,
            sync=sync,
            timing=SubmissionTiming(
                request_sent_at=sent_at,
                response_received_at=received_at,
                submit_call_ms=elapsed_ms,
                http_requests=self._request_log_since(log_start),
            ),
        )

    def dispatch_entries(
        self, planned: Sequence[PlannedEntry], session_date: date
    ) -> tuple[DispatchResult, ...]:
        """Compras limitadas `OPG` de las entradas aprobadas por el allocator."""
        return tuple(
            self.submit(self._planner.entry_order(entry, session_date))
            for entry in planned
        )

    def dispatch_protective_exit(
        self,
        planned: PlannedEntry,
        fill_price: Decimal,
        quantity: int,
        session_date: date,
    ) -> DispatchResult:
        """OCO SL / TP con los niveles recalculados sobre el precio ejecutado."""
        levels = self._planner.exit_levels_after_fill(planned, fill_price)
        return self.submit(
            self._planner.exit_order(
                planned.candidate.symbol, quantity, levels, session_date
            )
        )

    def dispatch_time_exit(
        self, symbol: str, quantity: int, session_date: date
    ) -> DispatchResult:
        return self.submit(
            self._planner.time_exit_order(symbol, quantity, session_date)
        )

    def sync(self, client_order_id: str) -> OrderSyncResult:
        """Lee el estado del broker y lo registra (incluidas ejecuciones)."""
        state = self._broker.get_order(client_order_id)

        if state is None:
            raise UnknownOrderError(client_order_id)

        return self._transaction(lambda: self._repository.apply_state(state))

    def sync_open_orders(self) -> tuple[OrderSyncResult, ...]:
        """Sincroniza las órdenes abiertas guardadas (las patas vienen con su padre)."""
        roots = [
            record.state.client_order_id
            for order in self._repository.list_open()
            if (record := self._repository.get_record(order.client_order_id))
            is not None
            and record.parent_client_order_id is None
        ]
        return tuple(self.sync(client_order_id) for client_order_id in roots)

    def cancel(self, client_order_id: str) -> OrderSyncResult:
        stored = self._repository.get(client_order_id)

        if stored is None:
            raise UnknownOrderError(client_order_id)

        self._broker.cancel_order(stored.broker_order_id)
        return self.sync(client_order_id)

    def wait_for_status(
        self,
        client_order_id: str,
        broker_statuses: Collection[str],
        timeout_seconds: float = 30.0,
        poll_interval_seconds: float = 0.25,
    ) -> StatusWaitResult:
        """
        Sondea el broker hasta que la orden llegue a uno de `broker_statuses`
        (o a un estado final). Registra cada cambio observado.
        """
        started = self._timer()
        transitions: list[OrderSyncResult] = []
        polls = 0

        while True:
            polls += 1
            result = self.sync(client_order_id)
            state = self._repository.get(client_order_id)
            assert state is not None

            if result.status_changed or result.executions:
                transitions.append(result)

            elapsed = self._timer() - started
            reached = state.broker_status in broker_statuses

            if reached or state.status.is_terminal or elapsed >= timeout_seconds:
                return StatusWaitResult(
                    state=state,
                    reached=reached,
                    polls=polls,
                    elapsed_ms=elapsed * 1000,
                    transitions=tuple(transitions),
                )

            self._sleep(poll_interval_seconds)

    def _transaction(self, operation: Callable[[], OrderSyncResult]) -> OrderSyncResult:
        try:
            result = operation()
            self._session.commit()
            return result
        except Exception:
            self._session.rollback()
            raise

    def _request_log_length(self) -> int:
        if isinstance(self._broker, RequestLoggingBroker):
            return len(self._broker.request_log)

        return 0

    def _request_log_since(self, start: int) -> tuple[RequestTiming, ...]:
        if isinstance(self._broker, RequestLoggingBroker):
            return self._broker.request_log[start:]

        return ()
