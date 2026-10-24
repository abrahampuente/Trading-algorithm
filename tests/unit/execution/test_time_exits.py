from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

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
    OrderSide,
    OrderState,
    OrderType,
    TimeInForce,
    normalize_alpaca_status,
)
from algo_trading.execution.pipeline import SignalEngine, SignalOrderPlanSource
from algo_trading.execution.service import ExecutionService
from algo_trading.persistence.models import Base
from algo_trading.portfolio import RiskParameters
from algo_trading.telemetry import AlertEmitter, MemoryAlertSink

ENTRY_DAY = date(2026, 9, 1)  # martes
HOLIDAY = date(2026, 9, 7)  # festivo en el calendario de prueba
COSTS = CostModel()
RISK = RiskParameters(commission_rate=COSTS.commission_rate)


class Calendar:
    name = "TEST"

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        days = []
        day = start

        while day <= end:
            if day.weekday() < 5 and day != HOLIDAY:
                days.append(session(day))
            day += timedelta(days=1)

        return tuple(days)


def session(day: date) -> TradingSession:
    return TradingSession(
        session_date=day,
        market_open=datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC),
        market_close=datetime(day.year, day.month, day.day, 20, tzinfo=UTC),
        is_early_close=False,
    )


def nth_session_after(start: date, sessions: int) -> date:
    """Fecha de la sesión que está `sessions` sesiones después de `start`."""
    days = [
        s.session_date for s in Calendar().sessions(start, start + timedelta(days=60))
    ]
    return days[sessions]


class FakeBroker:
    def __init__(self) -> None:
        self.states: dict[str, OrderState] = {}
        self.positions: dict[str, Decimal] = {}
        self.canceled: list[str] = []
        self.submitted: list[OrderRequest] = []

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_number="PA1",
            status="ACTIVE",
            currency="USD",
            cash=Decimal("90000"),
            equity=Decimal("100000"),
            buying_power=Decimal("400000"),
            trading_blocked=False,
        )

    def get_positions(self) -> Sequence[BrokerPosition]:
        return tuple(
            BrokerPosition(
                symbol=symbol,
                quantity=quantity,
                average_entry_price=Decimal("100"),
                market_value=quantity * Decimal("105"),
            )
            for symbol, quantity in self.positions.items()
            if quantity
        )

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
        client_order_id = broker_order_id.removeprefix("b-")
        self.canceled.append(client_order_id)
        state = self.states[client_order_id]
        self.states[client_order_id] = state.model_copy(
            update={
                "status": normalize_alpaca_status("canceled"),
                "broker_status": "canceled",
            }
        )

    def fill(self, request: OrderRequest, price: str, when: datetime) -> None:
        self.states[request.client_order_id] = self.state(
            request, "filled", str(request.quantity), price, when
        )
        sign = 1 if request.side is OrderSide.BUY else -1
        self.positions[request.symbol] = (
            self.positions.get(request.symbol, Decimal("0")) + sign * request.quantity
        )

    @staticmethod
    def state(
        request: OrderRequest,
        status: str,
        filled: str = "0",
        price: str | None = None,
        when: datetime | None = None,
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
            filled_at=when,
            updated_at=when,
        )


def buy(symbol: str, day: date, quantity: int = 10) -> OrderRequest:
    return OrderRequest(
        client_order_id=f"algo-entry-{symbol}-{day:%Y%m%d}",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.OPG,
        limit_price=Decimal("101"),
    )


def oco(symbol: str, quantity: int = 10) -> OrderRequest:
    return OrderRequest(
        client_order_id=f"algo-exit-{symbol}",
        symbol=symbol,
        side=OrderSide.SELL,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OCO,
        take_profit_limit_price=Decimal("115"),
        stop_loss_stop_price=Decimal("95"),
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def setup(
    engine: Engine, sink: MemoryAlertSink | None = None
) -> tuple[ExecutionService, FakeBroker, SignalOrderPlanSource, Session]:
    broker = FakeBroker()
    session = Session(engine)
    service = ExecutionService(broker, session, sleep=lambda seconds: None)
    source = SignalOrderPlanSource(
        SignalEngine(session),
        broker,
        Calendar(),
        ("AAPL", "MSFT"),
        RISK,
        COSTS,
        OrderPlanner(),
        alerts=AlertEmitter(sink) if sink is not None else None,
    )
    return service, broker, source, session


def open_position(
    service: ExecutionService, broker: FakeBroker, symbol: str, day: date
) -> None:
    entry = buy(symbol, day)
    service.submit(entry)
    broker.fill(
        entry, "100.50", datetime(day.year, day.month, day.day, 13, 30, 1, tzinfo=UTC)
    )
    service.sync(entry.client_order_id)
    protection = oco(symbol)
    service.submit(protection)


def test_open_position_entries_track_the_current_holding(engine: Engine) -> None:
    service, broker, _, _ = setup(engine)
    open_position(service, broker, "AAPL", ENTRY_DAY)
    sell = OrderRequest(
        client_order_id="algo-sell-AAPL",
        symbol="AAPL",
        side=OrderSide.SELL,
        quantity=10,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.OPG,
    )
    service.submit(sell)
    broker.fill(sell, "104", datetime(2026, 9, 10, 13, 30, tzinfo=UTC))
    service.sync(sell.client_order_id)

    assert service.open_position_entries() == {}

    second = date(2026, 9, 15)
    entry = buy("AAPL", second, quantity=7)
    service.submit(entry)
    broker.fill(entry, "103", datetime(2026, 9, 15, 13, 30, tzinfo=UTC))
    service.sync(entry.client_order_id)

    (current,) = service.open_position_entries().values()
    assert current.quantity == Decimal("7")
    assert current.opened_at == datetime(2026, 9, 15, 13, 30, tzinfo=UTC)


@pytest.mark.parametrize(("held", "expires"), [(19, False), (20, True), (21, True)])
def test_positions_expire_after_twenty_sessions(
    engine: Engine, held: int, expires: bool
) -> None:
    service, broker, source, _ = setup(engine)
    open_position(service, broker, "AAPL", ENTRY_DAY)
    target = nth_session_after(ENTRY_DAY, held)

    expiring = source.expiring_positions(service, session(target))

    assert bool(expiring) is expires

    if expires:
        (position,) = expiring
        assert position.sessions_held == held
        assert position.entry_session == ENTRY_DAY
        assert position.quantity == 10


def test_holidays_do_not_count_as_sessions(engine: Engine) -> None:
    service, broker, source, _ = setup(engine)
    open_position(service, broker, "AAPL", ENTRY_DAY)
    exit_day = nth_session_after(ENTRY_DAY, 20)

    # 20 sesiones de mercado son 29 días naturales por el fin de semana y el festivo.
    assert exit_day == date(2026, 9, 30)
    assert (
        source.expiring_positions(service, session(exit_day - timedelta(days=1))) == ()
    )
    assert source.expiring_positions(service, session(exit_day))


def test_expired_positions_are_sold_at_the_open_after_canceling_their_oco(
    engine: Engine,
) -> None:
    sink = MemoryAlertSink()
    service, broker, source, _ = setup(engine, sink)
    open_position(service, broker, "AAPL", ENTRY_DAY)
    open_position(service, broker, "MSFT", date(2026, 9, 20))
    service.halt("desajuste de reconciliación")  # las salidas reducen riesgo
    exit_session = session(nth_session_after(ENTRY_DAY, 20))

    (result,) = source.close_expired_positions(service, exit_session)

    assert broker.canceled == ["algo-exit-AAPL"]
    request = result.request
    assert (request.symbol, request.side, request.quantity) == (
        "AAPL",
        OrderSide.SELL,
        10,
    )
    assert (request.order_type, request.time_in_force) == (
        OrderType.MARKET,
        TimeInForce.OPG,
    )
    assert request.client_order_id.startswith("algo-time-AAPL-20260930-")
    assert sink.codes() == ["TIME_EXIT_SUBMITTED"]
    assert sink.alerts[0].details["entry_session"] == "2026-09-01"

    # Reintento en la misma apertura: el client_order_id ya existe, no se duplica.
    source.close_expired_positions(service, exit_session)
    assert [r.symbol for r in broker.submitted].count(
        "AAPL"
    ) == 3  # entrada, OCO, salida


def test_exiting_position_frees_its_slot_and_value_for_the_plan(engine: Engine) -> None:
    service, broker, source, _ = setup(engine)
    open_position(service, broker, "AAPL", ENTRY_DAY)
    exit_session = session(nth_session_after(ENTRY_DAY, 20))
    source.close_expired_positions(service, exit_session)

    plan = source.build_plan(exit_session, frozenset({"AAPL"}))

    assert plan.open_positions == ()
    assert plan.cash == Decimal("90000") + 10 * Decimal("105")


def test_post_close_announces_next_session_time_exits(engine: Engine) -> None:
    sink = MemoryAlertSink()
    service, broker, source, _ = setup(engine, sink)
    open_position(service, broker, "AAPL", ENTRY_DAY)
    closed = session(nth_session_after(ENTRY_DAY, 19))

    (expiring,) = source.announce_time_exits(service, closed)

    assert expiring.exit_session == date(2026, 9, 30)
    assert sink.codes() == ["TIME_EXIT_SCHEDULED"]
    assert source.announce_time_exits(service, session(ENTRY_DAY)) == ()


def test_positions_without_local_entry_are_left_to_reconciliation(
    engine: Engine,
) -> None:
    service, broker, source, _ = setup(engine)
    broker.positions["TSLA"] = Decimal("3")

    assert source.expiring_positions(service, session(date(2026, 12, 1))) == ()
