import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from algo_trading.execution.alpaca_broker import RequestTiming
from algo_trading.execution.orders import (
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from algo_trading.persistence.repositories.broker_order_repository import (
    BrokerOrderRecord,
)
from algo_trading.telemetry import (
    AlertEmitter,
    AlertLevel,
    LatencyTracker,
    LogAlertSink,
    MemoryAlertSink,
    latency_stats,
    operational_metrics,
    order_outcomes,
    order_slippage,
    percentile,
    slippage_bps,
    summarize_slippage,
)

NOW = datetime(2026, 10, 26, 20, 15, tzinfo=UTC)


def record(
    client_order_id: str,
    side: OrderSide = OrderSide.BUY,
    status: OrderStatus = OrderStatus.FILLED,
    filled: str = "10",
    average: str | None = "100.20",
    theoretical: str | None = "100",
) -> BrokerOrderRecord:
    return BrokerOrderRecord(
        state=OrderState(
            broker_order_id=f"b-{client_order_id}",
            client_order_id=client_order_id,
            symbol="AAPL",
            side=side,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.OPG,
            quantity=Decimal("10"),
            filled_quantity=Decimal(filled),
            filled_average_price=Decimal(average) if average else None,
            status=status,
            broker_status=status.value.lower(),
        ),
        parent_client_order_id=None,
        theoretical_price=Decimal(theoretical) if theoretical else None,
        strategy_id=None,
        signal_timestamp=None,
        take_profit_limit_price=None,
        stop_loss_stop_price=None,
        created_at=NOW,
        last_synced_at=NOW,
    )


def timing(latency: float, status: int | None = 200) -> RequestTiming:
    return RequestTiming(
        method="GET",
        path="/v2/orders",
        status=status,
        started_at=NOW,
        latency_ms=latency,
    )


# --- Slippage ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("side", "executed", "expected"),
    [
        (OrderSide.BUY, "100.20", "20"),  # compra más cara: adverso
        (OrderSide.BUY, "99.90", "-10"),  # compra más barata: favorable
        (OrderSide.SELL, "99.50", "50"),  # venta más barata: adverso
        (OrderSide.SELL, "100.10", "-10"),
    ],
)
def test_slippage_sign_convention(
    side: OrderSide, executed: str, expected: str
) -> None:
    assert slippage_bps(side, Decimal("100"), Decimal(executed)) == Decimal(expected)


def test_slippage_needs_a_positive_reference() -> None:
    with pytest.raises(ValueError, match="positivo"):
        slippage_bps(OrderSide.BUY, Decimal("0"), Decimal("1"))


def test_order_slippage_uses_stored_or_supplied_reference() -> None:
    records = [
        record("a"),
        record("b", average="101.00"),
        record("unfilled", status=OrderStatus.EXPIRED, filled="0", average=None),
        record("no-reference", theoretical=None),
    ]

    stored = order_slippage(records)
    # Apertura oficial de t+1 como referencia comparable con el backtest.
    vs_open = order_slippage(
        records, {"b": Decimal("100.80"), "no-reference": Decimal("100")}
    )

    assert [(s.client_order_id, s.slippage_bps) for s in stored] == [
        ("a", Decimal("20.00")),
        ("b", Decimal("100.00")),
    ]
    assert stored[0].slippage_cost == Decimal("2.00000000")
    assert [(s.client_order_id, s.slippage_bps) for s in vs_open] == [
        ("a", Decimal("20.00")),
        ("b", Decimal("19.84")),
        ("no-reference", Decimal("20.00")),
    ]


def test_slippage_summary() -> None:
    items = order_slippage(
        [
            record("a", average="100.20"),
            record("b", average="99.90"),
            record("c", side=OrderSide.SELL, average="99.50"),
        ]
    )

    summary = summarize_slippage(items)

    assert summary.orders == 3
    assert summary.mean_bps == Decimal("20.00")  # (20 - 10 + 50) / 3
    assert summary.median_bps == Decimal("20.00")
    assert summary.worst_bps == Decimal("50.00")
    assert summary.total_cost == Decimal("6.00000000")  # 2 - 1 + 5
    assert summary.weighted_bps == Decimal("20.00")  # 6 / 3.000 de nocional
    assert summarize_slippage(()).mean_bps is None


# --- Latencia ---------------------------------------------------------------


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 21)]

    assert percentile(values, 0.5) == 10
    assert percentile(values, 0.95) == 19
    assert percentile(values, 1.0) == 20
    assert percentile([], 0.5) is None

    with pytest.raises(ValueError):
        percentile(values, 0)


def test_latency_stats_count_errors() -> None:
    stats = latency_stats(
        [timing(400), timing(500), timing(600, 503), timing(900, 429), timing(0, None)]
    )

    assert stats.requests == 5
    assert stats.errors == 3
    assert stats.error_rate == pytest.approx(0.6)
    assert stats.mean_ms == pytest.approx(600)
    assert stats.p50_ms == 500
    assert stats.max_ms == 900


def test_latency_tracker_accumulates_a_growing_log() -> None:
    tracker = LatencyTracker()
    log = [timing(400), timing(420)]

    assert tracker.update(log) == 2
    log.append(timing(500))
    assert tracker.update(log) == 1
    assert tracker.update(log) == 0
    assert tracker.stats().requests == 3


# --- Tasa de acierto de órdenes ---------------------------------------------


def test_order_outcomes() -> None:
    outcomes = order_outcomes(
        [
            record("filled"),
            record("partial", status=OrderStatus.CANCELED, filled="4"),
            record("gap", status=OrderStatus.EXPIRED, filled="0", average=None),
            record("rejected", status=OrderStatus.REJECTED, filled="0", average=None),
            record("open", status=OrderStatus.SUBMITTED, filled="0", average=None),
            record("replaced", status=OrderStatus.REPLACED, filled="0", average=None),
        ]
    )

    assert (outcomes.total, outcomes.open) == (6, 1)
    assert (outcomes.filled, outcomes.partially_filled) == (1, 1)
    assert (outcomes.unfilled, outcomes.rejected) == (1, 1)
    assert outcomes.fill_rate == pytest.approx(0.25)
    assert outcomes.any_fill_rate == pytest.approx(0.5)


def test_operational_metrics_without_orders() -> None:
    metrics = operational_metrics([], [], clock=lambda: NOW)

    assert metrics.computed_at == NOW
    assert metrics.orders.fill_rate is None
    assert metrics.latency.mean_ms is None
    assert metrics.slippage.orders == 0


# --- Alertas ----------------------------------------------------------------


def test_alerts_go_to_every_sink_as_json(caplog: pytest.LogCaptureFixture) -> None:
    memory = MemoryAlertSink()
    emitter = AlertEmitter(memory, LogAlertSink(), clock=lambda: NOW)

    with caplog.at_level(logging.INFO, logger="algo_trading.alerts"):
        alert = emitter.emit(
            AlertLevel.CRITICAL,
            "INVENTORY_DRIFT",
            "AAPL: local 0, broker 3",
            {"symbol": "AAPL"},
        )

    assert memory.alerts == [alert]
    (logged,) = caplog.records
    assert logged.levelno == logging.CRITICAL
    payload = json.loads(logged.getMessage())
    assert payload == {
        "code": "INVENTORY_DRIFT",
        "details": {"symbol": "AAPL"},
        "level": "CRITICAL",
        "message": "AAPL: local 0, broker 3",
        "timestamp": "2026-10-26T20:15:00Z",
    }
