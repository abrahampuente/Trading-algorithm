from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from algo_trading.execution.alpaca_broker import RequestTiming
from algo_trading.execution.broker import AccountSnapshot, BrokerPosition
from algo_trading.execution.orders import (
    InvalidOrderTransitionError,
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
    normalize_alpaca_status,
)
from algo_trading.execution.service import ACCEPTED_BROKER_STATUSES, ExecutionService
from algo_trading.persistence.models import Base
from algo_trading.persistence.repositories.broker_order_repository import (
    BrokerOrderRepository,
    UnknownOrderError,
)
from algo_trading.portfolio import (
    EntryCandidate,
    PlannedEntry,
    PortfolioAllocator,
    PortfolioState,
    PositionSizer,
    RiskParameters,
)
from algo_trading.strategy import LongSignal

START = datetime(2024, 3, 5, 13, 0, tzinfo=UTC)
REQUEST = OrderRequest(
    client_order_id="algo-entry-SPY-test",
    symbol="SPY",
    side=OrderSide.BUY,
    quantity=1,
    order_type=OrderType.LIMIT,
    time_in_force=TimeInForce.DAY,
    limit_price=Decimal("300.00"),
    theoretical_price=Decimal("300.00"),
)


class FakeBroker:
    """Broker en memoria: cada consulta avanza un paso por `progression`."""

    def __init__(
        self,
        progression: Sequence[tuple[str, str, str | None]] = (("accepted", "0", None),),
    ) -> None:
        self.progression = list(progression)
        self.orders: dict[str, OrderRequest] = {}
        self.submitted: list[str] = []
        self.canceled: list[str] = []
        self._log: list[RequestTiming] = []
        self._step: dict[str, int] = {}

    @property
    def request_log(self) -> tuple[RequestTiming, ...]:
        return tuple(self._log)

    def get_account(self) -> AccountSnapshot:
        raise NotImplementedError

    def get_positions(self) -> Sequence[BrokerPosition]:
        return ()

    def submit_order(self, request: OrderRequest) -> OrderState:
        self._log.append(
            RequestTiming(
                method="GET",
                path="/v2/account",
                status=200,
                started_at=START,
                latency_ms=80.0,
            )
        )
        self._log.append(
            RequestTiming(
                method="POST",
                path="/v2/orders",
                status=200,
                started_at=START,
                latency_ms=123.4,
            )
        )
        self.orders[request.client_order_id] = request
        self.submitted.append(request.client_order_id)
        self._step[request.client_order_id] = 0
        return self._state(request, "pending_new", "0", None)

    def get_order(self, client_order_id: str) -> OrderState | None:
        request = self.orders.get(client_order_id)

        if request is None:
            return None

        step = min(self._step[client_order_id], len(self.progression) - 1)
        self._step[client_order_id] += 1
        status, filled, average = self.progression[step]

        if client_order_id in self.canceled:
            status = "canceled"

        return self._state(request, status, filled, average)

    def list_open_orders(self) -> Sequence[OrderState]:
        return ()

    def cancel_order(self, broker_order_id: str) -> None:
        self.canceled.append(broker_order_id.removeprefix("broker-"))

    @staticmethod
    def _state(
        request: OrderRequest, status: str, filled: str, average: str | None
    ) -> OrderState:
        return OrderState(
            broker_order_id=f"broker-{request.client_order_id}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            order_class=request.order_class,
            quantity=Decimal(request.quantity),
            filled_quantity=Decimal(filled),
            filled_average_price=Decimal(average) if average else None,
            limit_price=request.limit_price,
            status=normalize_alpaca_status(status),
            broker_status=status,
            submitted_at=START,
            updated_at=START + timedelta(milliseconds=350),
        )


class Clock:
    def __init__(self) -> None:
        self.now = START
        self.perf = 0.0

    def wall(self) -> datetime:
        self.now += timedelta(milliseconds=100)
        return self.now

    def timer(self) -> float:
        self.perf += 0.125
        return self.perf


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def service(
    engine: Engine, broker: FakeBroker, clock: Clock | None = None
) -> tuple[ExecutionService, Session, list[float]]:
    clock = clock or Clock()
    session = Session(engine)
    sleeps: list[float] = []
    return (
        ExecutionService(
            broker,
            session,
            clock=clock.wall,
            timer=clock.timer,
            sleep=sleeps.append,
        ),
        session,
        sleeps,
    )


def committed_repository(engine: Engine) -> BrokerOrderRepository:
    """Repositorio en otra sesión: sólo ve lo confirmado."""
    return BrokerOrderRepository(Session(engine))


def test_submit_records_order_and_timings(engine: Engine) -> None:
    broker = FakeBroker()
    execution, _, _ = service(engine, broker)

    result = execution.submit(REQUEST)

    assert not result.reused
    assert result.state.broker_status == "pending_new"
    timing = result.timing
    assert timing is not None
    assert timing.request_sent_at == START + timedelta(milliseconds=100)
    assert timing.response_received_at == START + timedelta(milliseconds=200)
    assert timing.submit_call_ms == pytest.approx(125)
    assert [request.path for request in timing.http_requests] == [
        "/v2/account",
        "/v2/orders",
    ]
    assert timing.order_request_latency_ms == pytest.approx(123.4)

    record = committed_repository(engine).get_record(REQUEST.client_order_id)
    assert record is not None
    assert record.created_at == timing.request_sent_at
    assert record.theoretical_price == Decimal("300.00")


def test_submit_is_idempotent_by_client_order_id(engine: Engine) -> None:
    broker = FakeBroker()
    execution, _, _ = service(engine, broker)

    execution.submit(REQUEST)
    again = execution.submit(REQUEST)

    assert again.reused
    assert again.timing is None
    assert broker.submitted == [REQUEST.client_order_id]


def test_wait_for_acceptance_polls_and_records_transitions(engine: Engine) -> None:
    broker = FakeBroker([("pending_new", "0", None), ("accepted", "0", None)])
    execution, _, sleeps = service(engine, broker)
    execution.submit(REQUEST)

    waited = execution.wait_for_status(
        REQUEST.client_order_id, ACCEPTED_BROKER_STATUSES, poll_interval_seconds=0.2
    )

    assert waited.reached
    assert waited.polls == 2
    assert waited.state.broker_status == "accepted"
    assert waited.elapsed_ms == pytest.approx(250)
    assert sleeps == [0.2]
    assert waited.transitions == ()  # pending_new → accepted: ambos SUBMITTED


def test_wait_records_fills_until_filled(engine: Engine) -> None:
    broker = FakeBroker(
        [
            ("accepted", "0", None),
            ("partially_filled", "4", "300.10"),
            ("filled", "10", "300.20"),
        ]
    )
    execution, _, _ = service(engine, broker)
    request = REQUEST.model_copy(update={"quantity": 10})
    execution.submit(request)

    waited = execution.wait_for_status(request.client_order_id, {"filled"})

    assert waited.reached
    assert waited.state.status is OrderStatus.FILLED
    assert [t.status for t in waited.transitions] == [
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
    ]
    executions = committed_repository(engine).get_executions(request.client_order_id)
    assert [(e.quantity, e.price) for e in executions] == [
        (Decimal("4"), Decimal("300.10")),
        (Decimal("6"), Decimal("300.26666667")),
    ]


def test_wait_stops_at_timeout(engine: Engine) -> None:
    broker = FakeBroker([("pending_new", "0", None)])
    execution, _, sleeps = service(engine, broker)
    execution.submit(REQUEST)

    waited = execution.wait_for_status(
        REQUEST.client_order_id, {"filled"}, timeout_seconds=0.5
    )

    assert not waited.reached
    assert waited.polls == 4
    assert len(sleeps) == 3


def test_cancel_uses_broker_id_and_records_the_final_state(engine: Engine) -> None:
    broker = FakeBroker()
    execution, _, _ = service(engine, broker)
    execution.submit(REQUEST)

    result = execution.cancel(REQUEST.client_order_id)

    assert broker.canceled == [REQUEST.client_order_id]
    assert result.status is OrderStatus.CANCELED
    assert committed_repository(engine).list_open() == ()


def test_unknown_orders_are_errors(engine: Engine) -> None:
    execution, _, _ = service(engine, FakeBroker())

    with pytest.raises(UnknownOrderError):
        execution.sync("missing")

    with pytest.raises(UnknownOrderError):
        execution.cancel("missing")


def test_failed_sync_is_rolled_back(engine: Engine) -> None:
    broker = FakeBroker([("filled", "1", "300.00"), ("accepted", "0", None)])
    execution, session, _ = service(engine, broker)
    execution.submit(REQUEST)
    execution.sync(REQUEST.client_order_id)

    with pytest.raises(InvalidOrderTransitionError):
        execution.sync(REQUEST.client_order_id)

    assert not session.dirty
    stored = committed_repository(engine).get(REQUEST.client_order_id)
    assert stored is not None
    assert stored.status is OrderStatus.FILLED


def planned_entry() -> PlannedEntry:
    signal = LongSignal(
        symbol="AAPL",
        signal_timestamp=datetime(2024, 3, 4, tzinfo=UTC),
        close=Decimal("175.10"),
        upper_band=Decimal("174.90"),
        previous_close=Decimal("173.00"),
        previous_upper_band=Decimal("174.50"),
        atr=Decimal("2.6789"),
        momentum=Decimal("0.05"),
    )
    risk = RiskParameters(
        risk_per_trade=Decimal("0.01"),
        max_position_exposure=Decimal("0.20"),
        stop_loss_atr_multiple=Decimal("2"),
        take_profit_atr_multiple=Decimal("3"),
        commission_rate=Decimal("0.0005"),
    )
    (planned,) = (
        PortfolioAllocator(PositionSizer(risk))
        .allocate(
            [EntryCandidate(signal=signal, entry_price=Decimal("175.20"))],
            PortfolioState(cash=Decimal("100000")),
        )
        .accepted
    )
    return planned


def test_dispatch_entries_and_protective_exit(engine: Engine) -> None:
    broker = FakeBroker()
    execution, _, _ = service(engine, broker)
    planned = planned_entry()

    (entry,) = execution.dispatch_entries([planned], date(2024, 3, 5))
    exit_order = execution.dispatch_protective_exit(
        planned, Decimal("176.00"), planned.sizing.quantity, date(2024, 3, 5)
    )
    time_exit = execution.dispatch_time_exit("AAPL", 5, date(2024, 4, 2))

    assert entry.request.time_in_force is TimeInForce.OPG
    assert entry.request.limit_price == Decimal("176.43")
    assert exit_order.request.order_class is OrderClass.OCO
    assert exit_order.request.stop_loss_stop_price == Decimal("170.65")
    assert time_exit.request.order_type is OrderType.MARKET
    assert len(broker.submitted) == 3

    synced = execution.sync_open_orders()
    assert {result.client_order_id for result in synced} == set(broker.submitted)
