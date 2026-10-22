from datetime import UTC, datetime
from decimal import Decimal

from algo_trading.execution import (
    CostModel,
    ExitDecision,
    ExitReason,
    OpenTrade,
    close_trade,
)
from algo_trading.portfolio import calculate_long_exit_levels

COSTS = CostModel()


def build_open_trade() -> OpenTrade:
    entry_executed = COSTS.buy_price(Decimal("100"))

    return OpenTrade(
        symbol="AAA",
        signal_timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        entry_timestamp=datetime(2024, 1, 2, tzinfo=UTC),
        entry_session_index=5,
        quantity=10,
        entry_theoretical_price=Decimal("100"),
        entry_executed_price=entry_executed,
        entry_commission=COSTS.commission(entry_executed, 10),
        exit_levels=calculate_long_exit_levels(entry_executed, Decimal("2")),
        planned_risk=Decimal("20"),
    )


def test_v1_base_cost_profile() -> None:
    assert COSTS.commission_rate == Decimal("0.0005")
    assert COSTS.slippage_rate == Decimal("0.0005")


def test_slippage_is_adverse_on_both_sides() -> None:
    assert COSTS.buy_price(Decimal("100")) == Decimal("100.05")
    assert COSTS.sell_price(Decimal("100")) == Decimal("99.95")


def test_commission_is_charged_on_executed_notional() -> None:
    assert COSTS.commission(Decimal("100.05"), 10) == Decimal("0.50025")


def test_close_trade_records_prices_costs_and_pnl() -> None:
    trade = close_trade(
        build_open_trade(),
        ExitDecision(
            reason=ExitReason.TAKE_PROFIT,
            theoretical_price=Decimal("104"),
        ),
        exit_timestamp=datetime(2024, 1, 5, tzinfo=UTC),
        exit_session_index=8,
        costs=COSTS,
    )

    assert trade.entry_theoretical_price == Decimal("100")
    assert trade.entry_executed_price == Decimal("100.05")
    assert trade.exit_theoretical_price == Decimal("104")
    assert trade.exit_executed_price == Decimal("103.948")
    assert trade.entry_commission == Decimal("0.50025")
    assert trade.exit_commission == Decimal("0.51974")
    assert trade.slippage_cost == Decimal("1.02")
    assert trade.total_costs == Decimal("2.03999")
    assert trade.gross_pnl == Decimal("40")
    assert trade.net_pnl == Decimal("37.96001")
    assert trade.net_pnl == (
        (trade.exit_executed_price - trade.entry_executed_price) * trade.quantity
        - trade.entry_commission
        - trade.exit_commission
    )
    assert trade.return_pct == Decimal("37.96001") / Decimal("1001.00025")
    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.sessions_since_entry == 3
    assert trade.stop_loss == Decimal("98.05")
    assert trade.take_profit == Decimal("104.05")
    assert trade.entry_atr == Decimal("2")


def test_losing_trade_includes_costs_in_net_pnl() -> None:
    trade = close_trade(
        build_open_trade(),
        ExitDecision(
            reason=ExitReason.STOP_LOSS,
            theoretical_price=Decimal("98.05"),
        ),
        exit_timestamp=datetime(2024, 1, 3, tzinfo=UTC),
        exit_session_index=6,
        costs=COSTS,
    )

    assert trade.gross_pnl == Decimal("-19.5")
    assert trade.net_pnl < trade.gross_pnl
    assert trade.net_pnl == trade.gross_pnl - trade.total_costs
