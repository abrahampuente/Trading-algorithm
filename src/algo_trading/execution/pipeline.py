"""
Pipeline de señales a órdenes para el paper trading (Hito 4).

Antes de la apertura de la sesión `S` (tarea `pre_open` del daemon):

1. Refresca los datos diarios hasta la última sesión cerrada `t` (ingesta
   de Yahoo, opcional e inyectable).
2. `SignalEngine` lee `market_bars_adjusted` de cada símbolo del universo y
   genera las señales con el mismo generador que el backtest (v1 por defecto);
   sólo valen las señales del cierre de `t`. Un símbolo sin la barra de `t`
   no genera señal (datos desfasados) y se informa.
3. `PortfolioAllocator` prioriza y dimensiona con el cash y las posiciones
   del broker (sin apalancamiento: se usa el cash, no el poder de compra) y
   los parámetros de riesgo de la v1. El precio de entrada estimado es
   `Close[t] · (1 + slippage)`: la apertura real no se conoce todavía.
4. `OrderPlanner` traduce cada entrada aprobada en una compra limitada `OPG`
   con el escudo anti-gap (`Close[t] + umbral · ATR[t]`).

Sin señales, el plan tiene 0 órdenes. La estrategia v1 no rebalancea: las
posiciones abiertas no generan órdenes de ajuste.

Protección tras la ejecución (`protect_filled_entries`, en cada sincronización
intradía): cuando una entrada del plan termina con cantidad ejecutada, envía
la OCO SL / TP con los niveles recalculados sobre el precio medio ejecutado;
si termina sin ejecución (gap mayor que el permitido), la descarta. Las
entradas pendientes se guardan en un `ProtectionStore` (MySQL en vivo:
`pending_protections`), así que un reinicio del daemon las retoma; las
posiciones del broker sin OCO ni entrada pendiente se alertan
(`UNPROTECTED_POSITIONS`).

Salidas temporales (`close_expired_positions`, antes de las entradas en
`pre_open`): como en el backtest, la sesión de entrada cuenta como la primera
y tras `max_holding_sessions = 20` sesiones la posición se cierra a mercado en
la apertura de la sesión 21 (`sesiones desde la entrada >= 20`). Antes se
cancela su OCO, que reserva las acciones. Al plan de entradas de esa misma
apertura, la posición que sale se le cuenta como cash a su valor de mercado
(en el backtest el cobro de la salida temporal también precede a las
entradas). `post_close` anuncia las salidas de la sesión siguiente
(`TIME_EXIT_SCHEDULED`).
"""

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.market_calendar import TradingCalendar, TradingSession
from algo_trading.execution.broker import BrokerAdapter
from algo_trading.execution.costs import CostModel
from algo_trading.execution.order_planner import OrderPlanner
from algo_trading.execution.orders import (
    OrderClass,
    OrderRequest,
    OrderSide,
)
from algo_trading.execution.service import DispatchResult, ExecutionService
from algo_trading.persistence.repositories.adjusted_bar_query_repository import (
    AdjustedMarketBarQueryRepository,
)
from algo_trading.persistence.repositories.live_state_repository import (
    ProtectionRecord,
    ProtectionResolution,
)
from algo_trading.portfolio.allocator import (
    EntryCandidate,
    PlannedEntry,
    PortfolioAllocator,
    RejectedCandidate,
)
from algo_trading.portfolio.portfolio_state import PortfolioState, Position
from algo_trading.portfolio.position_sizing import (
    MONEY_PRECISION,
    MONEY_ROUNDING,
    PositionSizer,
    RiskParameters,
)
from algo_trading.signals.base import SignalGenerator
from algo_trading.signals.momentum_breakout import (
    MomentumBreakoutGenerator,
    long_signal_from_signal,
)
from algo_trading.strategy.long_signal import LongSignal
from algo_trading.telemetry.alerts import AlertEmitter, AlertLevel

# Historial completo (los datos empiezan en 2020): el ATR de Wilder es
# recursivo y depende de toda la serie, así que una ventana corta daría un ATR
# (límite, SL / TP y tamaño) distinto del backtest.
DEFAULT_HISTORY_DAYS = 3650

DataRefresher = Callable[[Sequence[str], date], None]
DEFAULT_MAX_HOLDING_SESSIONS = 20


class ProtectionStore(Protocol):
    def add(self, record: ProtectionRecord) -> None: ...

    def pending(self) -> Sequence[ProtectionRecord]: ...

    def resolve(
        self,
        entry_client_order_id: str,
        resolution: ProtectionResolution,
        exit_client_order_id: str | None = None,
    ) -> None: ...


class InMemoryProtectionStore:
    """Almacén sin persistencia (tests y `--check-only`)."""

    def __init__(self) -> None:
        self._records: dict[str, ProtectionRecord] = {}

    def add(self, record: ProtectionRecord) -> None:
        self._records.setdefault(record.entry_client_order_id, record)

    def pending(self) -> tuple[ProtectionRecord, ...]:
        return tuple(r for r in self._records.values() if r.resolution is None)

    def resolve(
        self,
        entry_client_order_id: str,
        resolution: ProtectionResolution,
        exit_client_order_id: str | None = None,
    ) -> None:
        record = self._records[entry_client_order_id]
        self._records[entry_client_order_id] = record.model_copy(
            update={
                "resolution": resolution,
                "exit_client_order_id": exit_client_order_id,
            }
        )


class ExpiringPosition(BaseModel):
    """Posición que alcanza la salida temporal en `exit_session`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: int
    entry_session: date
    exit_session: date
    sessions_held: int


class SignalScan(BaseModel):
    """Señales al cierre de `signal_date` y símbolos sin datos de ese cierre."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_date: date
    signals: tuple[LongSignal, ...]
    stale: dict[str, date | None]


class SignalEngine:
    """Señales de la última sesión cerrada desde `market_bars_adjusted`."""

    def __init__(
        self,
        session: Session,
        generator: SignalGenerator | None = None,
        source: str = "yfinance",
        adjustment_version: str = "splits-v1",
        history_days: int = DEFAULT_HISTORY_DAYS,
    ) -> None:
        self._bars = AdjustedMarketBarQueryRepository(session)
        self._generator = generator or MomentumBreakoutGenerator()
        self._source = source
        self._adjustment_version = adjustment_version
        self._history = timedelta(days=history_days)

    def scan(self, symbols: Sequence[str], signal_date: date) -> SignalScan:
        start = datetime.combine(signal_date - self._history, datetime.min.time())
        end = datetime.combine(signal_date, datetime.max.time())
        signals: list[LongSignal] = []
        stale: dict[str, date | None] = {}

        for symbol in symbols:
            rows = self._bars.get_by_symbol(
                symbol=symbol,
                start=start,
                end=end,
                timeframe="1d",
                source=self._source,
                adjustment_version=self._adjustment_version,
            )
            bars = [
                MarketBar(
                    symbol=row.symbol,
                    timestamp=row.timestamp.replace(tzinfo=UTC),
                    timeframe=row.timeframe,
                    source=row.source,
                    open=row.open,
                    high=row.high,
                    low=row.low,
                    close=row.close,
                    volume=row.volume,
                )
                for row in rows
            ]

            if not bars or bars[-1].timestamp.date() != signal_date:
                stale[symbol] = bars[-1].timestamp.date() if bars else None
                continue

            signals.extend(
                long_signal_from_signal(signal)
                for signal in self._generator.generate(bars)
                if signal.signal_timestamp.date() == signal_date
            )

        return SignalScan(signal_date=signal_date, signals=tuple(signals), stale=stale)


class DailyPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: date
    signal_date: date
    generated_at: datetime
    signals: tuple[LongSignal, ...]
    entries: tuple[PlannedEntry, ...]
    rejected: tuple[RejectedCandidate, ...]
    orders: tuple[OrderRequest, ...]
    stale_symbols: dict[str, date | None]
    equity: Decimal
    cash: Decimal
    open_positions: tuple[str, ...]


class SignalOrderPlanSource:
    """`OrderPlanSource` del daemon: plan diario de entradas `OPG` desde señales."""

    def __init__(
        self,
        engine: SignalEngine,
        broker: BrokerAdapter,
        calendar: TradingCalendar,
        symbols: Sequence[str],
        risk: RiskParameters,
        costs: CostModel,
        planner: OrderPlanner,
        alerts: AlertEmitter | None = None,
        refresh_data: DataRefresher | None = None,
        submit_entries: bool = True,
        clock: Callable[[], datetime] | None = None,
        protections: ProtectionStore | None = None,
        max_holding_sessions: int = DEFAULT_MAX_HOLDING_SESSIONS,
    ) -> None:
        if max_holding_sessions < 1:
            raise ValueError("max_holding_sessions debe ser al menos 1")

        if risk.commission_rate != costs.commission_rate:
            raise ValueError("risk.commission_rate debe coincidir con la del CostModel")

        self._engine = engine
        self._broker = broker
        self._calendar = calendar
        self._symbols = tuple(symbols)
        self._allocator = PortfolioAllocator(PositionSizer(risk))
        self._costs = costs
        self._planner = planner
        self._alerts = alerts
        self._refresh = refresh_data
        self._submit_entries = submit_entries
        self._clock = clock or (lambda: datetime.now(UTC))
        self._protections = protections or InMemoryProtectionStore()
        self._max_holding = max_holding_sessions
        self._exiting: dict[date, frozenset[str]] = {}
        self.last_plan: DailyPlan | None = None

    @property
    def pending_entries(self) -> dict[str, ProtectionRecord]:
        """Entradas enviadas a la espera de ejecución, por `client_order_id`."""
        return {
            record.entry_client_order_id: record
            for record in self._protections.pending()
        }

    def signal_date_for(self, session: TradingSession) -> date:
        """Última sesión cerrada antes de `session` (el cierre `t` de la señal)."""
        previous = self._calendar.sessions(
            session.session_date - timedelta(days=10),
            session.session_date - timedelta(days=1),
        )

        if not previous:
            raise ValueError(f"Sin sesión anterior a {session.session_date}")

        return previous[-1].session_date

    def build_plan(
        self, session: TradingSession, exiting: frozenset[str] = frozenset()
    ) -> DailyPlan:
        """
        `exiting`: símbolos con salida temporal en esta apertura. No cuentan
        como posición abierta y su valor de mercado se suma al cash.
        """
        signal_date = self.signal_date_for(session)

        if self._refresh is not None:
            self._refresh(self._symbols, signal_date)

        scan = self._engine.scan(self._symbols, signal_date)
        account = self._broker.get_account()
        held = tuple(
            Position(
                symbol=position.symbol,
                quantity=int(position.quantity),
                entry_price=position.average_entry_price,
                # El allocator sólo usa símbolo, cantidad y valor de mercado.
                stop_loss=position.average_entry_price,
                take_profit=position.average_entry_price,
                market_price=(
                    position.market_value / position.quantity
                    if position.market_value
                    else position.average_entry_price
                ),
            )
            for position in self._broker.get_positions()
            if position.quantity >= 1
        )
        released = sum(
            (p.market_value for p in held if p.symbol in exiting), Decimal("0")
        )
        state = PortfolioState(
            cash=max(account.cash, Decimal("0")) + released,
            positions=tuple(p for p in held if p.symbol not in exiting),
        )

        with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
            candidates = [
                EntryCandidate(
                    signal=signal, entry_price=self._costs.buy_price(signal.close)
                )
                for signal in scan.signals
            ]

        allocation = self._allocator.allocate(candidates, state) if candidates else None
        entries = allocation.accepted if allocation is not None else ()
        orders = tuple(
            self._planner.entry_order(entry, session.session_date) for entry in entries
        )

        plan = DailyPlan(
            session_date=session.session_date,
            signal_date=signal_date,
            generated_at=self._clock(),
            signals=scan.signals,
            entries=entries,
            rejected=allocation.rejected if allocation is not None else (),
            orders=orders,
            stale_symbols=scan.stale,
            equity=state.equity,
            cash=state.cash,
            open_positions=tuple(sorted(state.open_symbols)),
        )
        self.last_plan = plan
        return plan

    def orders_for_session(self, session: TradingSession) -> Sequence[OrderRequest]:
        plan = self.build_plan(
            session, self._exiting.get(session.session_date, frozenset())
        )
        self._notify_plan(plan)

        if not self._submit_entries:
            return ()

        for order, entry in zip(plan.orders, plan.entries, strict=True):
            levels = entry.exit_levels
            self._protections.add(
                ProtectionRecord(
                    entry_client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    session_date=session.session_date,
                    entry_atr=levels.entry_atr,
                    stop_loss_atr_multiple=levels.stop_loss_atr_multiple,
                    take_profit_atr_multiple=levels.take_profit_atr_multiple,
                    signal_timestamp=entry.candidate.signal.signal_timestamp,
                    created_at=self._clock(),
                )
            )

        return plan.orders

    def expiring_positions(
        self, service: ExecutionService, session: TradingSession
    ) -> tuple[ExpiringPosition, ...]:
        """
        Posiciones con `sesiones desde la entrada >= max_holding_sessions` en
        `session` (regla del backtest: salida en la apertura de la sesión 21).
        La cantidad es la del broker; la fecha de entrada, la del primer fill
        local de la posición abierta.
        """
        entries = service.open_position_entries()
        broker_quantity = {
            p.symbol: p.quantity for p in self._broker.get_positions() if p.quantity > 0
        }
        expiring: list[ExpiringPosition] = []

        for symbol, entry in entries.items():
            if symbol not in broker_quantity:
                continue

            entry_session = entry.opened_at.date()
            sessions = self._calendar.sessions(entry_session, session.session_date)
            held = sum(
                1
                for s in sessions
                if entry_session < s.session_date <= session.session_date
            )

            if held >= self._max_holding:
                expiring.append(
                    ExpiringPosition(
                        symbol=symbol,
                        quantity=int(broker_quantity[symbol]),
                        entry_session=entry_session,
                        exit_session=session.session_date,
                        sessions_held=held,
                    )
                )

        return tuple(expiring)

    def close_expired_positions(
        self, service: ExecutionService, session: TradingSession
    ) -> tuple[DispatchResult, ...]:
        """Cancela la OCO y vende a mercado `OPG` las posiciones vencidas."""
        dispatched: list[DispatchResult] = []
        expiring = self.expiring_positions(service, session)

        for position in expiring:
            for order in service.open_orders():
                if (
                    order.symbol == position.symbol
                    and order.side is OrderSide.SELL
                    and order.order_class is OrderClass.OCO
                ):
                    service.cancel(order.client_order_id)
                    service.wait_for_status(
                        order.client_order_id, {"canceled"}, timeout_seconds=15
                    )

            result = service.dispatch_time_exit(
                position.symbol, position.quantity, session.session_date
            )
            dispatched.append(result)
            self._alert(
                AlertLevel.INFO,
                "TIME_EXIT_SUBMITTED",
                f"{position.symbol}: salida temporal x{position.quantity} a la "
                f"apertura ({position.sessions_held} sesiones desde "
                f"{position.entry_session})",
                {
                    "symbol": position.symbol,
                    "quantity": position.quantity,
                    "client_order_id": result.request.client_order_id,
                    "entry_session": position.entry_session.isoformat(),
                },
            )

        self._exiting[session.session_date] = frozenset(p.symbol for p in expiring)
        return tuple(dispatched)

    def announce_time_exits(
        self, service: ExecutionService, closed_session: TradingSession
    ) -> tuple[ExpiringPosition, ...]:
        """Alerta las salidas temporales de la apertura siguiente (`post_close`)."""
        upcoming = self._calendar.sessions(
            closed_session.session_date + timedelta(days=1),
            closed_session.session_date + timedelta(days=10),
        )

        if not upcoming:
            return ()

        next_session = upcoming[0]
        expiring = self.expiring_positions(service, next_session)

        if expiring:
            self._alert(
                AlertLevel.INFO,
                "TIME_EXIT_SCHEDULED",
                f"Salidas temporales en la apertura de {next_session.session_date}: "
                + ", ".join(p.symbol for p in expiring),
                {"symbols": [p.symbol for p in expiring]},
            )

        return expiring

    def protect_filled_entries(
        self, service: ExecutionService, session_date: date
    ) -> tuple[DispatchResult, ...]:
        """OCO SL / TP para las entradas del plan ya cerradas con ejecución."""
        dispatched: list[DispatchResult] = []

        for record in self._protections.pending():
            client_order_id = record.entry_client_order_id
            state = service.order_state(client_order_id)

            if state is None:
                # Nunca llegó al broker (p. ej. envío bloqueado): se descarta
                # al pasar su sesión.
                if record.session_date < session_date:
                    self._protections.resolve(
                        client_order_id, ProtectionResolution.NOT_SUBMITTED
                    )
                continue

            if not state.status.is_terminal:
                continue

            if state.filled_quantity < 1 or state.filled_average_price is None:
                self._protections.resolve(
                    client_order_id, ProtectionResolution.NOT_FILLED
                )
                self._alert(
                    AlertLevel.INFO,
                    "ENTRY_NOT_FILLED",
                    f"{state.symbol}: entrada {state.status.value} sin ejecución",
                    {"client_order_id": client_order_id},
                )
                continue

            result = service.dispatch_protective_exit_from(
                record.symbol,
                record.entry_atr,
                record.stop_loss_atr_multiple,
                record.take_profit_atr_multiple,
                state.filled_average_price,
                int(state.filled_quantity),
                session_date,
            )
            self._protections.resolve(
                client_order_id,
                ProtectionResolution.PROTECTED,
                result.request.client_order_id,
            )
            dispatched.append(result)
            self._alert(
                AlertLevel.INFO,
                "PROTECTIVE_EXIT_SUBMITTED",
                f"{state.symbol}: OCO SL {result.request.stop_loss_stop_price} / "
                f"TP {result.request.take_profit_limit_price}",
                {
                    "entry": client_order_id,
                    "exit": result.request.client_order_id,
                    "quantity": int(state.filled_quantity),
                    "fill_price": str(state.filled_average_price),
                },
            )

        return tuple(dispatched)

    def unprotected_positions(self, service: ExecutionService) -> tuple[str, ...]:
        """Posiciones del broker sin OCO de salida abierta (p. ej. tras reiniciar)."""
        protected = {
            order.symbol
            for order in service.open_orders()
            if order.side is OrderSide.SELL and order.order_class is OrderClass.OCO
        }
        pending = {record.symbol for record in self._protections.pending()}
        return tuple(
            sorted(
                position.symbol
                for position in self._broker.get_positions()
                if position.quantity > 0
                and position.symbol not in protected
                and position.symbol not in pending
            )
        )

    def _notify_plan(self, plan: DailyPlan) -> None:
        self._alert(
            AlertLevel.WARNING if plan.stale_symbols else AlertLevel.INFO,
            "PLAN_GENERATED",
            f"Plan {plan.session_date}: {len(plan.orders)} órdenes OPG de "
            f"{len(plan.signals)} señales del cierre {plan.signal_date}"
            + ("" if self._submit_entries else " (sólo plan, sin envío)"),
            {
                "orders": [
                    {
                        "symbol": order.symbol,
                        "quantity": order.quantity,
                        "limit_price": str(order.limit_price),
                    }
                    for order in plan.orders
                ],
                "rejected": [
                    {"symbol": item.candidate.symbol, "reason": item.reason.value}
                    for item in plan.rejected
                ],
                "stale_symbols": {
                    symbol: day.isoformat() if day else None
                    for symbol, day in plan.stale_symbols.items()
                },
                "equity": str(plan.equity),
                "open_positions": list(plan.open_positions),
            },
        )

    def _alert(
        self,
        level: AlertLevel,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> None:
        if self._alerts is not None:
            self._alerts.emit(level, code, message, details)
