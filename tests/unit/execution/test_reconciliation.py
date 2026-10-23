from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from algo_trading.data.market_calendar import TradingSession
from algo_trading.execution.alpaca_broker import RequestTiming
from algo_trading.execution.broker import AccountSnapshot, BrokerPosition
from algo_trading.execution.daemon import ExecutionJobs
from algo_trading.execution.orders import (
    ExecutionSource,
    OrderExecution,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from algo_trading.execution.service import ExecutionService, TradingHaltedError
from algo_trading.persistence.models import Base
from algo_trading.persistence.repositories.broker_order_repository import (
    BrokerOrderRepository,
)
from algo_trading.telemetry import AlertEmitter, AlertLevel, MemoryAlertSink

NOW = datetime(2026, 10, 26, 20, 15, tzinfo=UTC)
SESSION = TradingSession(
    session_date=date(2026, 10, 26),
    market_open=datetime(2026, 10, 26, 13, 30, tzinfo=UTC),
    market_close=datetime(2026, 10, 26, 20, 0, tzinfo=UTC),
    is_early_close=False,
)


def request(
    symbol: str, side: OrderSide = OrderSide.BUY, quantity: int = 10
) -> OrderRequest:
    return OrderRequest(
        client_order_id=f"algo-{side.value}-{symbol}",
        symbol=symbol,
        side=side,
        quantity=quantity,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.OPG,
        limit_price=Decimal("100"),
        theoretical_price=Decimal("100"),
    )


def state(
    order: OrderRequest,
    broker_status: str,
    filled: str = "0",
    average: str | None = None,
) -> OrderState:
    statuses = {
        "accepted": OrderStatus.SUBMITTED,
        "filled": OrderStatus.FILLED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "canceled": OrderStatus.CANCELED,
    }
    return OrderState(
        broker_order_id=f"broker-{order.client_order_id}",
        client_order_id=order.client_order_id,
        symbol=order.symbol,
        side=order.side,
        order_type=order.order_type,
        time_in_force=order.time_in_force,
        quantity=Decimal(order.quantity),
        filled_quantity=Decimal(filled),
        filled_average_price=Decimal(average) if average else None,
        limit_price=order.limit_price,
        status=statuses[broker_status],
        broker_status=broker_status,
        updated_at=NOW,
    )


class FakeBroker:
    def __init__(self) -> None:
        self.states: dict[str, OrderState] = {}
        self.positions: list[BrokerPosition] = []
        self.extra_open: list[OrderState] = []
        self.log: list[RequestTiming] = []

    @property
    def request_log(self) -> tuple[RequestTiming, ...]:
        return tuple(self.log)

    def get_account(self) -> AccountSnapshot:
        raise NotImplementedError

    def get_positions(self) -> Sequence[BrokerPosition]:
        return tuple(self.positions)

    def submit_order(self, order: OrderRequest) -> OrderState:
        self.log.append(
            RequestTiming(
                method="POST",
                path="/v2/orders",
                status=200,
                started_at=NOW,
                latency_ms=410.0,
            )
        )
        submitted = state(order, "accepted")
        self.states[order.client_order_id] = submitted
        return submitted

    def get_order(self, client_order_id: str) -> OrderState | None:
        self.log.append(
            RequestTiming(
                method="GET",
                path="/v2/orders",
                status=200,
                started_at=NOW,
                latency_ms=450.0,
            )
        )
        return self.states.get(client_order_id)

    def list_open_orders(self) -> Sequence[OrderState]:
        return tuple(
            s for s in self.states.values() if not s.status.is_terminal
        ) + tuple(self.extra_open)

    def cancel_order(self, broker_order_id: str) -> None:
        raise NotImplementedError

    def fill(self, order: OrderRequest, quantity: str, price: str) -> None:
        status = "filled" if Decimal(quantity) == order.quantity else "partially_filled"
        self.states[order.client_order_id] = state(order, status, quantity, price)
        symbol_qty = sum(
            (
                (s.filled_quantity if s.side is OrderSide.BUY else -s.filled_quantity)
                for s in self.states.values()
                if s.symbol == order.symbol
            ),
            Decimal("0"),
        )
        self.positions = [p for p in self.positions if p.symbol != order.symbol]
        if symbol_qty:
            self.positions.append(
                BrokerPosition(
                    symbol=order.symbol,
                    quantity=symbol_qty,
                    average_entry_price=Decimal(price),
                )
            )


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def build(
    engine: Engine,
) -> tuple[ExecutionService, FakeBroker, MemoryAlertSink, AlertEmitter]:
    broker = FakeBroker()
    sink = MemoryAlertSink()
    service = ExecutionService(broker, Session(engine), clock=lambda: NOW)
    return service, broker, sink, AlertEmitter(sink, clock=lambda: NOW)


def test_consistent_inventory_after_fills(engine: Engine) -> None:
    service, broker, sink, alerts = build(engine)
    buy, sell = request("AAPL"), request("AAPL", OrderSide.SELL, 4)
    service.submit(buy)
    broker.fill(buy, "10", "100.10")
    service.submit(sell)
    broker.fill(sell, "4", "101.00")

    report = service.reconcile_positions(alerts)

    assert report.local_positions == {"AAPL": Decimal("6")}
    assert report.broker_positions == {"AAPL": Decimal("6")}
    assert report.is_consistent
    assert service.halt_reason is None
    assert sink.codes() == ["RECONCILIATION_OK"]


def test_inventory_drift_halts_new_orders(engine: Engine) -> None:
    service, broker, sink, alerts = build(engine)
    buy = request("AAPL")
    service.submit(buy)
    broker.fill(buy, "10", "100.10")
    # Posición abierta fuera del sistema (p. ej. desde el panel de Alpaca).
    broker.positions.append(
        BrokerPosition(
            symbol="MSFT", quantity=Decimal("3"), average_entry_price=Decimal("400")
        )
    )

    report = service.reconcile_positions(alerts)

    (drift,) = report.drifts
    assert (drift.symbol, drift.local_quantity, drift.broker_quantity) == (
        "MSFT",
        Decimal("0"),
        Decimal("3"),
    )
    assert drift.difference == Decimal("3")
    assert report.requires_halt
    assert sink.codes() == ["INVENTORY_DRIFT", "TRADING_HALTED"]
    assert sink.alerts[0].level is AlertLevel.CRITICAL
    assert sink.alerts[0].details["difference"] == "3"

    with pytest.raises(TradingHaltedError, match="reconciliación"):
        service.submit(request("XOM"))

    service.resume()
    service.submit(request("XOM"))


def test_reconciliation_syncs_fills_before_comparing(engine: Engine) -> None:
    service, broker, _, _ = build(engine)
    buy = request("AAPL")
    service.submit(buy)
    broker.fill(buy, "10", "100.10")  # aún no sincronizado en MySQL

    without_sync = service.reconcile_positions(sync_first=False)
    service.resume()
    with_sync = service.reconcile_positions()

    assert without_sync.drifts and without_sync.requires_halt
    assert with_sync.is_consistent


def test_unrecorded_fills_are_detected(engine: Engine) -> None:
    service, broker, sink, alerts = build(engine)
    buy = request("AAPL")
    service.submit(buy)
    broker.fill(buy, "10", "100.10")
    repository_session = Session(engine)
    repository = BrokerOrderRepository(repository_session)
    # Fill aplicado sin registrar la ejecución (fuente de actividades sin cargar).
    repository.apply_state(broker.states[buy.client_order_id], deduce_executions=False)
    repository_session.commit()

    report = service.reconcile_positions(alerts, sync_first=False)

    assert report.unrecorded_fills == (buy.client_order_id,)
    assert report.requires_halt
    assert "UNRECORDED_FILLS" in sink.codes()

    repository.record_execution(
        OrderExecution(
            execution_id="activity-1",
            client_order_id=buy.client_order_id,
            executed_at=NOW,
            quantity=Decimal("10"),
            price=Decimal("100.10"),
            cumulative_quantity=Decimal("10"),
            source=ExecutionSource.BROKER_ACTIVITY,
        )
    )
    repository_session.commit()
    service.resume()

    assert service.reconcile_positions(sync_first=False).is_consistent


def test_unknown_broker_orders_and_stale_local_orders(engine: Engine) -> None:
    service, broker, sink, alerts = build(engine)
    tracked = request("AAPL")
    service.submit(tracked)
    broker.extra_open.append(state(request("TSLA"), "accepted"))
    # El broker ya no tiene abierta la orden registrada y la sincronización
    # todavía no lo reflejó.
    broker.states[tracked.client_order_id] = state(tracked, "canceled")

    report = service.reconcile_positions(alerts, sync_first=False)

    assert report.unknown_broker_orders == ("algo-buy-TSLA",)
    assert report.stale_local_orders == (tracked.client_order_id,)
    assert report.requires_halt
    assert sink.codes() == [
        "UNKNOWN_BROKER_ORDERS",
        "STALE_LOCAL_ORDERS",
        "TRADING_HALTED",
    ]


def test_stale_local_orders_alone_only_warn(engine: Engine) -> None:
    service, broker, sink, alerts = build(engine)
    tracked = request("AAPL")
    service.submit(tracked)
    broker.states[tracked.client_order_id] = state(tracked, "canceled")

    report = service.reconcile_positions(alerts, sync_first=False)

    assert not report.requires_halt
    assert not report.is_consistent
    assert service.halt_reason is None
    assert [a.level for a in sink.alerts] == [AlertLevel.WARNING]


# --- Tareas del daemon -----------------------------------------------------


class Plans:
    def __init__(self, orders: Sequence[OrderRequest]) -> None:
        self.orders = orders

    def orders_for_session(self, session: TradingSession) -> Sequence[OrderRequest]:
        return self.orders


def test_execution_jobs_cycle(engine: Engine) -> None:
    service, broker, sink, alerts = build(engine)
    buy = request("AAPL")
    jobs = ExecutionJobs(
        service, Plans([buy]), alerts, request_log=lambda: broker.request_log
    )

    jobs.pre_open(SESSION)
    broker.fill(buy, "10", "100.20")
    jobs.intraday(SESSION)
    jobs.post_close(SESSION)

    assert sink.codes() == ["PRE_OPEN_SUBMITTED", "RECONCILIATION_OK", "DAILY_SUMMARY"]
    assert jobs.last_reconciliation is not None
    assert jobs.last_reconciliation.is_consistent
    metrics = jobs.last_metrics
    assert metrics is not None
    assert metrics.slippage.orders == 1
    assert metrics.slippage.mean_bps == Decimal("20.00")  # 100.20 frente a 100
    assert metrics.orders.fill_rate == 1.0
    assert metrics.latency.requests == len(broker.log)
    summary = sink.alerts[-1].details
    assert summary["slippage_mean_bps"] == "20.00"
    assert summary["fill_rate"] == 1.0


def test_pre_open_is_blocked_while_halted(engine: Engine) -> None:
    service, broker, sink, alerts = build(engine)
    service.halt("desajuste de reconciliación")
    jobs = ExecutionJobs(service, Plans([request("AAPL")]), alerts)

    jobs.pre_open(SESSION)

    assert broker.states == {}
    assert sink.codes() == ["ORDERS_BLOCKED"]
    assert sink.alerts[0].level is AlertLevel.CRITICAL
