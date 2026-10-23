from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from algo_trading.execution.order_planner import OrderPlanner, client_order_id
from algo_trading.execution.orders import (
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
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

SESSION = date(2024, 3, 5)
SIGNAL = LongSignal(
    symbol="AAPL",
    signal_timestamp=datetime(2024, 3, 4, tzinfo=UTC),
    close=Decimal("175.10"),
    upper_band=Decimal("174.90"),
    previous_close=Decimal("173.00"),
    previous_upper_band=Decimal("174.50"),
    atr=Decimal("2.6789"),
    momentum=Decimal("0.05"),
)
RISK = RiskParameters(
    risk_per_trade=Decimal("0.01"),
    max_position_exposure=Decimal("0.20"),
    stop_loss_atr_multiple=Decimal("2"),
    take_profit_atr_multiple=Decimal("3"),
    commission_rate=Decimal("0.0005"),
)


def planned_entry() -> PlannedEntry:
    allocation = PortfolioAllocator(PositionSizer(RISK)).allocate(
        [EntryCandidate(signal=SIGNAL, entry_price=Decimal("175.20"))],
        PortfolioState(cash=Decimal("100000")),
    )
    (planned,) = allocation.accepted
    return planned


def test_entry_is_an_opening_limit_order_capped_by_the_gap_shield() -> None:
    planned = planned_entry()

    order = OrderPlanner(gap_threshold_atr=Decimal("0.5")).entry_order(planned, SESSION)

    # 175.10 + 0.5 x 2.6789 = 176.43945 → hacia abajo al centavo.
    assert order.limit_price == Decimal("176.43")
    assert (order.side, order.order_type, order.time_in_force) == (
        OrderSide.BUY,
        OrderType.LIMIT,
        TimeInForce.OPG,
    )
    assert order.quantity == planned.sizing.quantity
    assert order.theoretical_price == SIGNAL.close
    assert order.signal_timestamp == SIGNAL.signal_timestamp
    assert order.strategy_id == "momentum_breakout_v1"


def test_client_order_ids_are_deterministic_and_distinct_by_intent() -> None:
    planner = OrderPlanner()
    planned = planned_entry()

    first = planner.entry_order(planned, SESSION)
    again = planner.entry_order(planned, SESSION)
    next_day = planner.entry_order(planned, date(2024, 3, 6))
    exit_order = planner.exit_order(
        "AAPL", 10, planner.exit_levels_after_fill(planned, Decimal("176")), SESSION
    )

    assert first.client_order_id == again.client_order_id
    assert first.client_order_id.startswith("algo-entry-AAPL-20240305-")
    assert first.client_order_id != next_day.client_order_id
    assert first.client_order_id != exit_order.client_order_id
    assert len(first.client_order_id) <= 128
    assert client_order_id("entry", "AAPL", SESSION, "other") != first.client_order_id


def test_exit_levels_use_the_fill_price_and_the_frozen_signal_atr() -> None:
    planned = planned_entry()

    levels = OrderPlanner().exit_levels_after_fill(planned, Decimal("176.00"))

    assert levels.entry_price == Decimal("176.00")
    assert levels.entry_atr == SIGNAL.atr
    assert levels.stop_loss == Decimal("176.00") - 2 * SIGNAL.atr
    assert levels.take_profit == Decimal("176.00") + 3 * SIGNAL.atr


def test_exit_is_a_gtc_oco_rounded_prudently() -> None:
    planner = OrderPlanner()
    levels = planner.exit_levels_after_fill(planned_entry(), Decimal("176.00"))

    order = planner.exit_order("AAPL", 11, levels, SESSION)

    # SL 170.6422 → 170.65 (stop más alto); TP 184.0367 → 184.04.
    assert order.order_class is OrderClass.OCO
    assert (order.side, order.order_type, order.time_in_force) == (
        OrderSide.SELL,
        OrderType.LIMIT,
        TimeInForce.GTC,
    )
    assert order.stop_loss_stop_price == Decimal("170.65")
    assert order.take_profit_limit_price == Decimal("184.04")
    assert order.quantity == 11
    assert order.client_order_id.startswith("algo-exit-AAPL-")


def test_time_exit_is_a_market_order_at_the_open() -> None:
    order = OrderPlanner().time_exit_order("AAPL", 11, date(2024, 4, 2))

    assert (order.side, order.order_type, order.time_in_force) == (
        OrderSide.SELL,
        OrderType.MARKET,
        TimeInForce.OPG,
    )
    assert order.limit_price is None
    assert order.client_order_id.startswith("algo-time-AAPL-20240402-")


def test_negative_gap_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="gap_threshold_atr"):
        OrderPlanner(gap_threshold_atr=Decimal("-0.1"))
