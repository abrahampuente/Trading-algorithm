from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from algo_trading.data.market_calendar import TradingSession
from algo_trading.execution.broker import AccountSnapshot, BrokerPosition
from algo_trading.execution.costs import CostModel
from algo_trading.execution.order_planner import OrderPlanner
from algo_trading.execution.orders import (
    OrderClass,
    OrderRequest,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
    normalize_alpaca_status,
)
from algo_trading.execution.pipeline import SignalEngine, SignalOrderPlanSource
from algo_trading.execution.service import ExecutionService
from algo_trading.persistence.models import Base
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted
from algo_trading.portfolio import RejectionReason, RiskParameters
from algo_trading.telemetry import AlertEmitter, MemoryAlertSink

FRIDAY = date(2026, 10, 23)
MONDAY_SESSION = TradingSession(
    session_date=date(2026, 10, 26),
    market_open=datetime(2026, 10, 26, 13, 30, tzinfo=UTC),
    market_close=datetime(2026, 10, 26, 20, 0, tzinfo=UTC),
    is_early_close=False,
)
COSTS = CostModel()
RISK = RiskParameters(
    max_positions=5,
    risk_per_trade=Decimal("0.01"),
    max_position_exposure=Decimal("0.20"),
    stop_loss_atr_multiple=Decimal("2.0"),
    take_profit_atr_multiple=Decimal("3.0"),
    commission_rate=COSTS.commission_rate,
)


class WeekdayCalendar:
    name = "TEST"

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        days = []
        day = start

        while day <= end:
            if day.weekday() < 5:
                days.append(
                    TradingSession(
                        session_date=day,
                        market_open=datetime(
                            day.year, day.month, day.day, 13, 30, tzinfo=UTC
                        ),
                        market_close=datetime(
                            day.year, day.month, day.day, 20, tzinfo=UTC
                        ),
                        is_early_close=False,
                    )
                )
            day += timedelta(days=1)

        return tuple(days)


def rows(
    symbol: str, last_day: date, breakout: bool, count: int = 30
) -> list[MarketBarAdjusted]:
    """Tendencia suave y, si `breakout`, ruptura alcista en la última barra."""
    days = [last_day - timedelta(days=offset) for offset in range(count)][::-1]
    result = []

    for index, day in enumerate(days):
        close = Decimal("100") + Decimal("0.1") * index

        if breakout and index == count - 1:
            close = Decimal("110")

        result.append(
            MarketBarAdjusted(
                symbol=symbol,
                timestamp=datetime(day.year, day.month, day.day),
                timeframe="1d",
                source="yfinance",
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=Decimal("1000"),
                adjustment_factor=Decimal("1"),
                adjustment_version="splits-v1",
            )
        )

    return result


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)

    with Session(database) as session:
        session.add_all(rows("AAPL", FRIDAY, breakout=True))
        session.add_all(rows("MSFT", FRIDAY, breakout=True))
        session.add_all(rows("XOM", FRIDAY, breakout=False))
        session.add_all(
            rows("JPM", FRIDAY - timedelta(days=1), breakout=True)
        )  # desfasado
        session.commit()

    yield database
    database.dispose()


class FakeBroker:
    def __init__(self, positions: Sequence[BrokerPosition] = ()) -> None:
        self.positions = list(positions)
        self.states: dict[str, OrderState] = {}
        self.submitted: list[OrderRequest] = []

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_number="PA1",
            status="ACTIVE",
            currency="USD",
            cash=Decimal("100000"),
            equity=Decimal("100000"),
            buying_power=Decimal("400000"),
            trading_blocked=False,
        )

    def get_positions(self) -> Sequence[BrokerPosition]:
        return tuple(self.positions)

    def submit_order(self, request: OrderRequest) -> OrderState:
        self.submitted.append(request)
        state = self.state(request, "accepted")
        self.states[request.client_order_id] = state
        return state

    def get_order(self, client_order_id: str) -> OrderState | None:
        return self.states.get(client_order_id)

    def list_open_orders(self) -> Sequence[OrderState]:
        return tuple(s for s in self.states.values() if not s.status.is_terminal)

    def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError

    @staticmethod
    def state(
        request: OrderRequest, status: str, filled: str = "0", price: str | None = None
    ) -> OrderState:
        return OrderState(
            broker_order_id=f"b-{request.client_order_id}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            order_class=request.order_class,
            quantity=Decimal(request.quantity),
            filled_quantity=Decimal(filled),
            filled_average_price=Decimal(price) if price else None,
            limit_price=request.limit_price or request.take_profit_limit_price,
            status=normalize_alpaca_status(status),
            broker_status=status,
        )


def plan_source(
    session: Session,
    broker: FakeBroker,
    sink: MemoryAlertSink | None = None,
    submit_entries: bool = True,
    refreshes: list[tuple[tuple[str, ...], date]] | None = None,
) -> SignalOrderPlanSource:
    return SignalOrderPlanSource(
        SignalEngine(session),
        broker,
        WeekdayCalendar(),
        ("AAPL", "MSFT", "XOM", "JPM"),
        RISK,
        COSTS,
        OrderPlanner(gap_threshold_atr=Decimal("0.5")),
        alerts=AlertEmitter(sink) if sink is not None else None,
        refresh_data=(
            (lambda symbols, end: refreshes.append((tuple(symbols), end)))
            if refreshes is not None
            else None
        ),
        submit_entries=submit_entries,
    )


# --- Motor de señales -------------------------------------------------------


def test_signal_engine_uses_only_the_last_closed_session(engine: Engine) -> None:
    with Session(engine) as session:
        scan = SignalEngine(session).scan(("AAPL", "XOM", "JPM", "TSLA"), FRIDAY)

    assert [signal.symbol for signal in scan.signals] == ["AAPL"]
    assert scan.signals[0].signal_timestamp == datetime(2026, 10, 23, tzinfo=UTC)
    assert scan.stale == {"JPM": date(2026, 10, 22), "TSLA": None}


# --- Plan diario ------------------------------------------------------------


def test_plan_turns_signals_into_opening_limit_orders(engine: Engine) -> None:
    refreshes: list[tuple[tuple[str, ...], date]] = []
    sink = MemoryAlertSink()

    with Session(engine) as session:
        source = plan_source(session, FakeBroker(), sink, refreshes=refreshes)
        orders = source.orders_for_session(MONDAY_SESSION)

    plan = source.last_plan
    assert plan is not None
    assert refreshes == [(("AAPL", "MSFT", "XOM", "JPM"), FRIDAY)]
    assert plan.signal_date == FRIDAY
    assert {order.symbol for order in orders} == {"AAPL", "MSFT"}

    for order, entry in zip(orders, plan.entries, strict=True):
        signal = entry.candidate.signal
        assert (order.order_type, order.time_in_force) == (
            OrderType.LIMIT,
            TimeInForce.OPG,
        )
        assert order.limit_price == (
            signal.close + Decimal("0.5") * signal.atr
        ).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        assert order.quantity == entry.sizing.quantity > 0
        assert order.client_order_id.startswith(f"algo-entry-{order.symbol}-20261026-")
        # Sizing sobre el cierre con slippage: la apertura aún no se conoce.
        assert entry.candidate.entry_price == COSTS.buy_price(signal.close)

    assert plan.stale_symbols == {"JPM": date(2026, 10, 22)}
    assert set(source.pending_entries) == {order.client_order_id for order in orders}
    (alert,) = sink.alerts
    assert alert.code == "PLAN_GENERATED"
    assert alert.level.value == "WARNING"  # hay datos desfasados
    assert len(alert.details["orders"]) == 2


def test_no_signals_produce_an_empty_plan(engine: Engine) -> None:
    sink = MemoryAlertSink()

    with Session(engine) as session:
        source = SignalOrderPlanSource(
            SignalEngine(session),
            FakeBroker(),
            WeekdayCalendar(),
            ("XOM",),
            RISK,
            COSTS,
            OrderPlanner(),
            alerts=AlertEmitter(sink),
        )
        orders = source.orders_for_session(MONDAY_SESSION)

    assert orders == ()
    assert source.pending_entries == {}
    assert sink.alerts[0].message.startswith(
        "Plan 2026-10-26: 0 órdenes OPG de 0 señales"
    )
    assert sink.alerts[0].level.value == "INFO"


def test_open_positions_are_not_entered_again(engine: Engine) -> None:
    broker = FakeBroker(
        positions=[
            BrokerPosition(
                symbol="AAPL",
                quantity=Decimal("50"),
                average_entry_price=Decimal("105"),
                market_value=Decimal("5500"),
            )
        ]
    )

    with Session(engine) as session:
        source = plan_source(session, broker)
        orders = source.orders_for_session(MONDAY_SESSION)

    plan = source.last_plan
    assert plan is not None
    assert [order.symbol for order in orders] == ["MSFT"]
    assert [(r.candidate.symbol, r.reason) for r in plan.rejected] == [
        ("AAPL", RejectionReason.ALREADY_OPEN)
    ]
    assert plan.open_positions == ("AAPL",)
    assert plan.equity == Decimal("105500")  # cash + valor de mercado


def test_plan_only_generates_but_returns_no_orders(engine: Engine) -> None:
    sink = MemoryAlertSink()

    with Session(engine) as session:
        source = plan_source(session, FakeBroker(), sink, submit_entries=False)
        orders = source.orders_for_session(MONDAY_SESSION)

    assert orders == ()
    assert source.last_plan is not None
    assert len(source.last_plan.orders) == 2
    assert source.pending_entries == {}
    assert "(sólo plan, sin envío)" in sink.alerts[0].message


def test_mismatched_commission_is_rejected(engine: Engine) -> None:
    with Session(engine) as session, pytest.raises(ValueError, match="commission_rate"):
        SignalOrderPlanSource(
            SignalEngine(session),
            FakeBroker(),
            WeekdayCalendar(),
            ("AAPL",),
            RISK,
            CostModel(commission_rate=Decimal("0.001")),
            OrderPlanner(),
        )


# --- Salidas de protección --------------------------------------------------


def test_filled_entries_get_a_protective_oco_even_when_halted(engine: Engine) -> None:
    broker = FakeBroker()
    sink = MemoryAlertSink()

    with Session(engine) as session:
        source = plan_source(session, broker, sink)
        service = ExecutionService(broker, session)
        aapl, msft = source.orders_for_session(MONDAY_SESSION)

        for order in (aapl, msft):
            service.submit(order)

        broker.states[aapl.client_order_id] = broker.state(
            aapl, "filled", str(aapl.quantity), "110.20"
        )
        broker.states[msft.client_order_id] = broker.state(msft, "expired")
        service.sync_open_orders()
        service.halt("desajuste de reconciliación")

        dispatched = source.protect_filled_entries(service, MONDAY_SESSION.session_date)

        assert source.pending_entries == {}
        assert source.unprotected_positions(service) == ()

    (exit_order,) = dispatched
    request = exit_order.request
    plan = source.last_plan
    assert plan is not None
    atr = plan.entries[0].candidate.signal.atr
    assert request.order_class is OrderClass.OCO
    assert request.quantity == aapl.quantity
    assert request.stop_loss_stop_price == (Decimal("110.20") - 2 * atr).quantize(
        Decimal("0.01"), rounding="ROUND_CEILING"
    )
    assert request.take_profit_limit_price == (Decimal("110.20") + 3 * atr).quantize(
        Decimal("0.01"), rounding="ROUND_CEILING"
    )
    assert sink.codes()[1:] == ["PROTECTIVE_EXIT_SUBMITTED", "ENTRY_NOT_FILLED"]


def test_open_entries_wait_and_unprotected_positions_are_reported(
    engine: Engine,
) -> None:
    broker = FakeBroker()

    with Session(engine) as session:
        source = plan_source(session, broker)
        service = ExecutionService(broker, session)
        orders = source.orders_for_session(MONDAY_SESSION)
        service.submit(orders[0])

        assert source.protect_filled_entries(service, MONDAY_SESSION.session_date) == ()
        assert len(source.pending_entries) == 2

        broker.positions.append(
            BrokerPosition(
                symbol="TSLA", quantity=Decimal("5"), average_entry_price=Decimal("200")
            )
        )
        assert source.unprotected_positions(service) == ("TSLA",)


def test_order_state_from_service_matches_broker_after_sync(engine: Engine) -> None:
    broker = FakeBroker()

    with Session(engine) as session:
        service = ExecutionService(broker, session)
        source = plan_source(session, broker)
        (order, _) = source.orders_for_session(MONDAY_SESSION)
        service.submit(order)

        state = service.order_state(order.client_order_id)

    assert state is not None
    assert state.status is OrderStatus.SUBMITTED


def test_live_signals_match_the_backtest_generator_exactly() -> None:
    """Paridad con el backtest: el ATR de Wilder usa toda la serie."""
    import random

    from algo_trading.strategy import LongSignalGenerator

    generator = random.Random(7)
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    close = Decimal("100")
    series = []
    start = date(2025, 1, 1)

    for offset in range(500):
        close = max(Decimal("10"), close + Decimal(generator.randint(-150, 250)) / 100)
        day = start + timedelta(days=offset)
        series.append(
            MarketBarAdjusted(
                symbol="AAA",
                timestamp=datetime(day.year, day.month, day.day),
                timeframe="1d",
                source="yfinance",
                open=close,
                high=close + 1,
                low=close - 1,
                close=close,
                volume=Decimal("1000"),
                adjustment_factor=Decimal("1"),
                adjustment_version="splits-v1",
            )
        )

    with Session(database) as session:
        session.add_all(series)
        session.commit()
        engine = SignalEngine(session)
        bars = engine._bars.get_by_symbol(  # noqa: SLF001 - mismas filas que el motor
            "AAA",
            datetime(2020, 1, 1),
            datetime(2030, 1, 1),
            source="yfinance",
            adjustment_version="splits-v1",
        )
        from algo_trading.data.market_bar import MarketBar

        market_bars = [
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
            for row in bars
        ]
        backtest = LongSignalGenerator().generate(market_bars)
        late = [
            signal
            for signal in backtest
            if signal.signal_timestamp.date() > date(2026, 1, 1)
        ]
        assert late

        for signal in late:
            (live,) = engine.scan(("AAA",), signal.signal_timestamp.date()).signals
            assert live == signal

    database.dispose()
