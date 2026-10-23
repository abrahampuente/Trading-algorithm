from collections.abc import Callable
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from algo_trading.execution.orders import (
    TERMINAL_STATUSES,
    ExecutionSource,
    InvalidOrderTransitionError,
    OrderClass,
    OrderExecution,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
    validate_transition,
)
from algo_trading.persistence.models.broker_orders import (
    BrokerOrderModel,
    OrderExecutionModel,
)

QUANTUM = Decimal("0.00000001")


class UnknownOrderError(LookupError):
    """No hay ninguna orden guardada con ese `client_order_id`."""


class BrokerOrderRecord(BaseModel):
    """Orden guardada: estado del broker y contexto de la estrategia."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: OrderState
    parent_client_order_id: str | None
    theoretical_price: Decimal | None
    strategy_id: str | None
    signal_timestamp: datetime | None
    take_profit_limit_price: Decimal | None
    stop_loss_stop_price: Decimal | None
    created_at: datetime
    last_synced_at: datetime


class OrderSyncResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    created: bool
    previous_status: OrderStatus | None
    status: OrderStatus
    executions: tuple[OrderExecution, ...] = ()

    @property
    def status_changed(self) -> bool:
        return self.previous_status is not self.status


class BrokerOrderRepository:
    """
    Persistencia de órdenes del broker (`broker_orders`) y sus ejecuciones
    (`order_executions`).

    - `record_submission` guarda la orden enviada con el contexto de la
      solicitud (precio teórico, estrategia, patas TP / SL).
    - `apply_state` registra un estado del broker: valida la transición con
      `validate_transition` (y que la cantidad ejecutada no baje), actualiza
      la fila y, si la cantidad ejecutada creció, deduce la ejecución del
      incremento. Las patas (`legs`) se guardan como órdenes hijas.
    - `record_execution` guarda un fill informado por el broker (idempotente
      por `execution_id`). Use una sola fuente de fills por orden: actividades
      del broker o deducción desde `apply_state` (`deduce_executions=False`).

    No confirma la transacción. Timestamps en UTC sin zona en MySQL; se
    devuelven en UTC con zona. Cantidades y precios a 8 decimales.
    """

    def __init__(
        self,
        session: Session,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))

    def record_submission(
        self,
        request: OrderRequest,
        state: OrderState,
        created_at: datetime | None = None,
    ) -> OrderSyncResult:
        """`created_at`: momento local del envío (por defecto, ahora)."""
        if request.client_order_id != state.client_order_id:
            raise ValueError("La solicitud y el estado son de órdenes distintas")

        result = self.apply_state(state, created_at=created_at)
        row = self._row(state.client_order_id)
        assert row is not None
        row.theoretical_price = _quantize(request.theoretical_price)
        row.strategy_id = request.strategy_id
        row.signal_timestamp = _to_db(request.signal_timestamp)
        row.take_profit_limit_price = _quantize(request.take_profit_limit_price)
        row.stop_loss_stop_price = _quantize(request.stop_loss_stop_price)
        self._session.flush()
        return result

    def apply_state(
        self,
        state: OrderState,
        parent_client_order_id: str | None = None,
        deduce_executions: bool = True,
        created_at: datetime | None = None,
    ) -> OrderSyncResult:
        now = _to_db(self._clock())
        assert now is not None
        row = self._row(state.client_order_id)
        previous_status = OrderStatus(row.status) if row is not None else None
        executions: tuple[OrderExecution, ...] = ()

        if row is None:
            row = BrokerOrderModel(
                client_order_id=state.client_order_id,
                parent_client_order_id=parent_client_order_id,
                filled_quantity=Decimal("0"),
                filled_average_price=None,
                created_at=_to_db(created_at) or now,
            )
            self._session.add(row)
        else:
            assert previous_status is not None
            validate_transition(previous_status, state.status)

            if state.filled_quantity < row.filled_quantity:
                raise InvalidOrderTransitionError(
                    f"{state.client_order_id}: la cantidad ejecutada no puede bajar"
                )

        if deduce_executions and state.filled_quantity > row.filled_quantity:
            executions = (self._deduced_execution(row, state, now),)

        self._update(row, state, now)
        self._session.flush()

        for execution in executions:
            self._add_execution(row.id, execution)

        for leg in state.legs:
            self.apply_state(
                leg,
                parent_client_order_id=state.client_order_id,
                deduce_executions=deduce_executions,
            )

        self._session.flush()
        return OrderSyncResult(
            client_order_id=state.client_order_id,
            created=previous_status is None,
            previous_status=previous_status,
            status=state.status,
            executions=executions,
        )

    def record_execution(self, execution: OrderExecution) -> bool:
        """Guarda un fill del broker; devuelve False si ya estaba guardado."""
        row = self._row(execution.client_order_id)

        if row is None:
            raise UnknownOrderError(execution.client_order_id)

        exists = self._session.scalar(
            select(OrderExecutionModel.id).where(
                OrderExecutionModel.execution_id == execution.execution_id
            )
        )

        if exists is not None:
            return False

        self._add_execution(row.id, execution)
        self._session.flush()
        return True

    def get(self, client_order_id: str) -> OrderState | None:
        record = self.get_record(client_order_id)
        return record.state if record is not None else None

    def get_record(self, client_order_id: str) -> BrokerOrderRecord | None:
        row = self._row(client_order_id)
        return self._to_record(row) if row is not None else None

    def list_open(self) -> tuple[OrderState, ...]:
        """Órdenes no finales (incluidas patas), en orden de alta."""
        rows = self._session.scalars(
            select(BrokerOrderModel)
            .where(
                BrokerOrderModel.status.not_in(
                    [status.value for status in TERMINAL_STATUSES]
                )
            )
            .order_by(BrokerOrderModel.created_at, BrokerOrderModel.id)
        ).all()
        return tuple(self._to_state(row) for row in rows)

    def get_executions(self, client_order_id: str) -> tuple[OrderExecution, ...]:
        rows = self._session.execute(
            select(OrderExecutionModel, BrokerOrderModel.client_order_id)
            .join(BrokerOrderModel, BrokerOrderModel.id == OrderExecutionModel.order_id)
            .where(BrokerOrderModel.client_order_id == client_order_id)
            .order_by(OrderExecutionModel.executed_at, OrderExecutionModel.id)
        ).all()
        return tuple(
            OrderExecution(
                execution_id=execution.execution_id,
                client_order_id=order_id,
                executed_at=_from_db(execution.executed_at),
                quantity=execution.quantity,
                price=execution.price,
                cumulative_quantity=execution.cumulative_quantity,
                commission=execution.commission,
                source=ExecutionSource(execution.source),
            )
            for execution, order_id in rows
        )

    def _row(self, client_order_id: str) -> BrokerOrderModel | None:
        return self._session.scalar(
            select(BrokerOrderModel).where(
                BrokerOrderModel.client_order_id == client_order_id
            )
        )

    @staticmethod
    def _deduced_execution(
        row: BrokerOrderModel, state: OrderState, now: datetime
    ) -> OrderExecution:
        if state.filled_average_price is None:
            raise ValueError(
                f"{state.client_order_id}: cantidad ejecutada sin precio medio"
            )

        previous_quantity = row.filled_quantity
        previous_price = row.filled_average_price or Decimal("0")
        quantity = state.filled_quantity - previous_quantity

        with localcontext(prec=28, rounding=ROUND_HALF_EVEN):
            price = (
                state.filled_average_price * state.filled_quantity
                - previous_price * previous_quantity
            ) / quantity

        executed_at = (
            state.filled_at
            if state.status is OrderStatus.FILLED and state.filled_at is not None
            else state.updated_at
        ) or _from_db(now)

        return OrderExecution(
            execution_id=(
                f"{state.client_order_id}:{state.filled_quantity.normalize():f}"
            ),
            client_order_id=state.client_order_id,
            executed_at=executed_at,
            quantity=quantity,
            price=price.quantize(QUANTUM, rounding=ROUND_HALF_EVEN),
            cumulative_quantity=state.filled_quantity,
            source=ExecutionSource.ORDER_UPDATE,
        )

    def _add_execution(self, order_id: int, execution: OrderExecution) -> None:
        executed_at = _to_db(execution.executed_at)
        assert executed_at is not None
        self._session.add(
            OrderExecutionModel(
                order_id=order_id,
                execution_id=execution.execution_id,
                executed_at=executed_at,
                quantity=_required(execution.quantity),
                price=_required(execution.price),
                cumulative_quantity=_required(execution.cumulative_quantity),
                commission=_required(execution.commission),
                source=execution.source.value,
            )
        )

    @staticmethod
    def _update(row: BrokerOrderModel, state: OrderState, now: datetime) -> None:
        row.broker_order_id = state.broker_order_id
        row.symbol = state.symbol
        row.side = state.side.value
        row.quantity = _required(state.quantity)
        row.order_type = state.order_type.value
        row.time_in_force = state.time_in_force.value
        row.order_class = state.order_class.value
        row.limit_price = _quantize(state.limit_price)
        row.stop_price = _quantize(state.stop_price)
        row.status = state.status.value
        row.broker_status = state.broker_status
        row.filled_quantity = _required(state.filled_quantity)
        row.filled_average_price = _quantize(state.filled_average_price)
        row.submitted_at = _to_db(state.submitted_at)
        row.filled_at = _to_db(state.filled_at)
        row.broker_updated_at = _to_db(state.updated_at)
        row.last_synced_at = now

    def _to_record(self, row: BrokerOrderModel) -> BrokerOrderRecord:
        return BrokerOrderRecord(
            state=self._to_state(row),
            parent_client_order_id=row.parent_client_order_id,
            theoretical_price=row.theoretical_price,
            strategy_id=row.strategy_id,
            signal_timestamp=_optional_from_db(row.signal_timestamp),
            take_profit_limit_price=row.take_profit_limit_price,
            stop_loss_stop_price=row.stop_loss_stop_price,
            created_at=_from_db(row.created_at),
            last_synced_at=_from_db(row.last_synced_at),
        )

    def _to_state(self, row: BrokerOrderModel) -> OrderState:
        children = self._session.scalars(
            select(BrokerOrderModel)
            .where(BrokerOrderModel.parent_client_order_id == row.client_order_id)
            .order_by(BrokerOrderModel.id)
        ).all()
        return OrderState(
            broker_order_id=row.broker_order_id,
            client_order_id=row.client_order_id,
            symbol=row.symbol,
            side=OrderSide(row.side),
            order_type=OrderType(row.order_type),
            time_in_force=TimeInForce(row.time_in_force),
            order_class=OrderClass(row.order_class),
            quantity=row.quantity,
            filled_quantity=row.filled_quantity,
            filled_average_price=row.filled_average_price,
            limit_price=row.limit_price,
            stop_price=row.stop_price,
            status=OrderStatus(row.status),
            broker_status=row.broker_status,
            submitted_at=_optional_from_db(row.submitted_at),
            filled_at=_optional_from_db(row.filled_at),
            updated_at=_optional_from_db(row.broker_updated_at),
            legs=tuple(self._to_state(child) for child in children),
        )


def _to_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(tzinfo=None)


def _from_db(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC)


def _optional_from_db(value: datetime | None) -> datetime | None:
    return _from_db(value) if value is not None else None


def _quantize(value: Decimal | None) -> Decimal | None:
    return (
        value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN) if value is not None else None
    )


def _required(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)
