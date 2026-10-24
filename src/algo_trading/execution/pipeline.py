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
entradas pendientes viven en memoria: si el daemon se reinicia, las
posiciones sin OCO se alertan (`UNPROTECTED_POSITIONS`).
"""

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

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
    ) -> None:
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
        self._pending: dict[str, PlannedEntry] = {}
        self.last_plan: DailyPlan | None = None

    @property
    def pending_entries(self) -> dict[str, PlannedEntry]:
        """Entradas enviadas a la espera de ejecución, por `client_order_id`."""
        return dict(self._pending)

    def signal_date_for(self, session: TradingSession) -> date:
        """Última sesión cerrada antes de `session` (el cierre `t` de la señal)."""
        previous = self._calendar.sessions(
            session.session_date - timedelta(days=10),
            session.session_date - timedelta(days=1),
        )

        if not previous:
            raise ValueError(f"Sin sesión anterior a {session.session_date}")

        return previous[-1].session_date

    def build_plan(self, session: TradingSession) -> DailyPlan:
        signal_date = self.signal_date_for(session)

        if self._refresh is not None:
            self._refresh(self._symbols, signal_date)

        scan = self._engine.scan(self._symbols, signal_date)
        account = self._broker.get_account()
        positions = tuple(
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
        state = PortfolioState(
            cash=max(account.cash, Decimal("0")), positions=positions
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
        plan = self.build_plan(session)
        self._notify_plan(plan)

        if not self._submit_entries:
            return ()

        for order, entry in zip(plan.orders, plan.entries, strict=True):
            self._pending[order.client_order_id] = entry

        return plan.orders

    def protect_filled_entries(
        self, service: ExecutionService, session_date: date
    ) -> tuple[DispatchResult, ...]:
        """OCO SL / TP para las entradas del plan ya cerradas con ejecución."""
        dispatched: list[DispatchResult] = []

        for client_order_id, entry in list(self._pending.items()):
            state = service.order_state(client_order_id)

            if state is None or not state.status.is_terminal:
                continue

            del self._pending[client_order_id]

            if state.filled_quantity < 1 or state.filled_average_price is None:
                self._alert(
                    AlertLevel.INFO,
                    "ENTRY_NOT_FILLED",
                    f"{state.symbol}: entrada {state.status.value} sin ejecución",
                    {"client_order_id": client_order_id},
                )
                continue

            result = service.dispatch_protective_exit(
                entry,
                state.filled_average_price,
                int(state.filled_quantity),
                session_date,
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
        pending = {entry.candidate.symbol for entry in self._pending.values()}
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
