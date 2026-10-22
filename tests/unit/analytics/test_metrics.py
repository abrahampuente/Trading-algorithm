from datetime import UTC, datetime, timedelta
from decimal import Decimal

from algo_trading.analytics import (
    calculate_buy_and_hold,
    calculate_exposure,
    calculate_max_drawdown,
    calculate_trade_statistics,
)
from algo_trading.backtesting import BacktestParameters, BacktestResult, EquityPoint
from algo_trading.data.market_bar import MarketBar
from algo_trading.domain.corporate_actions.dto import CorporateDividend
from algo_trading.execution import (
    CostModel,
    ExitDecision,
    ExitReason,
    OpenTrade,
    Trade,
    close_trade,
)
from algo_trading.portfolio import calculate_long_exit_levels

START = datetime(2024, 1, 1, tzinfo=UTC)
ZERO_COSTS = CostModel(commission_rate=Decimal("0"), slippage_rate=Decimal("0"))


def day(offset: int) -> datetime:
    return START + timedelta(days=offset)


def build_trade(
    exit_price: str,
    reason: ExitReason = ExitReason.TAKE_PROFIT,
    sessions: int = 2,
    days: int = 3,
) -> Trade:
    """Entrada 100 x 10 unidades, riesgo planificado 20, sin costes."""
    open_trade = OpenTrade(
        symbol="AAA",
        signal_timestamp=day(0),
        entry_timestamp=day(1),
        entry_session_index=1,
        quantity=10,
        entry_theoretical_price=Decimal("100"),
        entry_executed_price=Decimal("100"),
        entry_commission=Decimal("0"),
        exit_levels=calculate_long_exit_levels(Decimal("100"), Decimal("2")),
        planned_risk=Decimal("20"),
    )

    return close_trade(
        open_trade,
        ExitDecision(reason=reason, theoretical_price=Decimal(exit_price)),
        exit_timestamp=day(1 + days),
        exit_session_index=1 + sessions,
        costs=ZERO_COSTS,
    )


def test_trade_statistics_with_known_values() -> None:
    trades = [
        build_trade("104", sessions=2, days=2),  # +40
        build_trade("102", sessions=4, days=6),  # +20
        build_trade("98", ExitReason.STOP_LOSS, sessions=1, days=1),  # -20
        build_trade("99", ExitReason.TIME_EXIT, sessions=20, days=28),  # -10
    ]

    stats = calculate_trade_statistics(trades)

    assert stats.total_trades == 4
    assert stats.winning_trades == 2
    assert stats.losing_trades == 2
    assert stats.win_rate == Decimal("0.5")
    assert stats.gross_profit == Decimal("60")
    assert stats.gross_loss == Decimal("30")
    assert stats.profit_factor == Decimal("2")
    assert stats.expectancy == Decimal("7.5")
    # R múltiplos: 2, 1, -1, -0.5.
    assert stats.expectancy_r == Decimal("0.375")
    assert stats.average_win == Decimal("30")
    assert stats.average_loss == Decimal("-15")
    assert stats.average_sessions_held == Decimal("6.75")
    assert stats.average_holding_days == Decimal("9.25")
    assert stats.exits_by_reason == {
        "STOP_LOSS": 1,
        "TAKE_PROFIT": 2,
        "TIME_EXIT": 1,
    }


def test_trade_statistics_without_trades_returns_none_ratios() -> None:
    stats = calculate_trade_statistics([])

    assert stats.total_trades == 0
    assert stats.win_rate is None
    assert stats.profit_factor is None
    assert stats.expectancy is None
    assert stats.average_sessions_held is None
    assert stats.exits_by_reason == {}


def test_profit_factor_is_none_without_losses() -> None:
    stats = calculate_trade_statistics([build_trade("104"), build_trade("101")])

    assert stats.win_rate == Decimal("1")
    assert stats.gross_loss == Decimal("0")
    assert stats.profit_factor is None


def test_breakeven_trade_counts_as_loss() -> None:
    stats = calculate_trade_statistics([build_trade("100"), build_trade("104")])

    assert stats.winning_trades == 1
    assert stats.losing_trades == 1
    assert stats.win_rate == Decimal("0.5")


def test_statistics_use_net_pnl_after_costs() -> None:
    open_trade = OpenTrade(
        symbol="AAA",
        signal_timestamp=day(0),
        entry_timestamp=day(1),
        entry_session_index=1,
        quantity=10,
        entry_theoretical_price=Decimal("100"),
        entry_executed_price=Decimal("100.05"),
        entry_commission=Decimal("0.50025"),
        exit_levels=calculate_long_exit_levels(Decimal("100.05"), Decimal("2")),
        planned_risk=Decimal("20"),
    )
    # Bruto +1 (100 → 100.1), pero los costes lo convierten en pérdida.
    trade = close_trade(
        open_trade,
        ExitDecision(
            reason=ExitReason.TIME_EXIT,
            theoretical_price=Decimal("100.1"),
        ),
        exit_timestamp=day(2),
        exit_session_index=2,
        costs=CostModel(),
    )

    stats = calculate_trade_statistics([trade])

    assert trade.gross_pnl > 0
    assert trade.net_pnl < 0
    assert stats.winning_trades == 0
    assert stats.total_costs == trade.total_costs


def test_max_drawdown_picks_deepest_relative_fall() -> None:
    points = [
        (day(1), Decimal("110")),
        (day(2), Decimal("99")),  # -10 % desde 110
        (day(3), Decimal("120")),
        (day(4), Decimal("96")),  # -20 % desde 120
        (day(5), Decimal("130")),
    ]

    drawdown = calculate_max_drawdown(points, initial_equity=Decimal("100"))

    assert drawdown.max_drawdown_pct == Decimal("0.2")
    assert drawdown.max_drawdown_amount == Decimal("24")
    assert drawdown.peak_timestamp == day(3)
    assert drawdown.trough_timestamp == day(4)


def test_max_drawdown_uses_initial_equity_as_first_peak() -> None:
    drawdown = calculate_max_drawdown(
        [(day(1), Decimal("90")), (day(2), Decimal("95"))],
        initial_equity=Decimal("100"),
    )

    assert drawdown.max_drawdown_pct == Decimal("0.1")
    assert drawdown.peak_timestamp is None
    assert drawdown.trough_timestamp == day(1)


def test_max_drawdown_is_zero_for_rising_curve() -> None:
    drawdown = calculate_max_drawdown(
        [(day(index), Decimal(100 + index)) for index in range(5)],
        initial_equity=Decimal("100"),
    )

    assert drawdown.max_drawdown_pct == 0
    assert drawdown.trough_timestamp is None


def test_exposure_from_equity_curve() -> None:
    curve = (
        EquityPoint(
            timestamp=day(1),
            cash=Decimal("1000"),
            market_value=Decimal("0"),
            equity=Decimal("1000"),
            open_positions=0,
        ),
        EquityPoint(
            timestamp=day(2),
            cash=Decimal("500"),
            market_value=Decimal("500"),
            equity=Decimal("1000"),
            open_positions=1,
        ),
        EquityPoint(
            timestamp=day(3),
            cash=Decimal("800"),
            market_value=Decimal("200"),
            equity=Decimal("1000"),
            open_positions=1,
        ),
        EquityPoint(
            timestamp=day(4),
            cash=Decimal("1000"),
            market_value=Decimal("0"),
            equity=Decimal("1000"),
            open_positions=0,
        ),
    )
    result = BacktestResult(
        parameters=BacktestParameters(initial_cash=Decimal("1000")),
        trades=(),
        gap_filtered=(),
        rejected=(),
        equity_curve=curve,
        final_cash=Decimal("1000"),
    )

    exposure = calculate_exposure(result)

    assert exposure.time_in_market == Decimal("0.5")
    assert exposure.average_exposure == Decimal("0.175")


def build_bar(symbol: str, offset: int, open_price: str, close: str) -> MarketBar:
    open_value = Decimal(open_price)
    close_value = Decimal(close)

    return MarketBar(
        symbol=symbol,
        timestamp=day(offset),
        timeframe="1d",
        source="test",
        open=open_value,
        high=max(open_value, close_value),
        low=min(open_value, close_value),
        close=close_value,
        volume=Decimal("1000"),
    )


def test_buy_and_hold_equal_weight_without_costs() -> None:
    bars = {
        "AAA": [
            build_bar("AAA", 0, "100", "100"),
            build_bar("AAA", 1, "100", "80"),
            build_bar("AAA", 2, "80", "120"),
        ],
        "BBB": [
            build_bar("BBB", 0, "50", "50"),
            build_bar("BBB", 1, "50", "50"),
            build_bar("BBB", 2, "50", "45"),
        ],
    }

    comparison = calculate_buy_and_hold(
        bars,
        initial_equity=Decimal("1000"),
        costs=ZERO_COSTS,
        strategy_return=Decimal("0.08"),
    )

    assert comparison is not None
    # AAA +20 %, BBB -10 % con 500 cada uno.
    assert comparison.final_equity == Decimal("1050")
    assert comparison.cumulative_return == Decimal("0.05")
    assert comparison.strategy_excess_return == Decimal("0.03")
    # Día 1: 400 + 500 = 900 → -10 % desde 1000.
    assert comparison.max_drawdown.max_drawdown_pct == Decimal("0.1")
    assert comparison.max_drawdown.trough_timestamp == day(1)


def test_buy_and_hold_applies_entry_and_exit_costs() -> None:
    bars = {
        "AAA": [
            build_bar("AAA", 0, "100", "100"),
            build_bar("AAA", 1, "100", "110"),
        ]
    }

    comparison = calculate_buy_and_hold(
        bars,
        initial_equity=Decimal("1000"),
        costs=CostModel(),
        strategy_return=Decimal("0"),
    )

    factor = Decimal("1.0005")
    expected = (
        Decimal("1000")
        / (Decimal("100") * factor * factor)
        * Decimal("110")
        * Decimal("0.9995")
        * Decimal("0.9995")
    )
    assert comparison is not None
    assert comparison.final_equity == expected
    assert comparison.cumulative_return < Decimal("0.1")


def test_buy_and_hold_handles_symbols_starting_later() -> None:
    bars = {
        "AAA": [build_bar("AAA", 0, "100", "100"), build_bar("AAA", 1, "100", "100")],
        "BBB": [build_bar("BBB", 1, "50", "60")],
    }

    comparison = calculate_buy_and_hold(
        bars,
        initial_equity=Decimal("1000"),
        costs=ZERO_COSTS,
        strategy_return=Decimal("0"),
    )

    assert comparison is not None
    assert comparison.final_equity == Decimal("1100")
    assert comparison.max_drawdown.max_drawdown_pct == 0


def test_buy_and_hold_without_bars_is_none() -> None:
    assert (
        calculate_buy_and_hold(
            {},
            initial_equity=Decimal("1000"),
            costs=ZERO_COSTS,
            strategy_return=Decimal("0"),
        )
        is None
    )


def test_buy_and_hold_credits_dividends_after_purchase() -> None:
    bars = {
        "AAA": [
            build_bar("AAA", 0, "100", "100"),
            build_bar("AAA", 1, "100", "100"),
            build_bar("AAA", 2, "100", "100"),
        ]
    }
    dividends = {
        "AAA": [
            # Ex-date el día de compra: no se cobra.
            CorporateDividend(
                symbol="AAA", ex_date=day(0), amount=Decimal("5"), source="t"
            ),
            CorporateDividend(
                symbol="AAA", ex_date=day(1), amount=Decimal("2"), source="t"
            ),
            # Ex-date posterior a la última barra: no se cobra.
            CorporateDividend(
                symbol="AAA", ex_date=day(5), amount=Decimal("5"), source="t"
            ),
        ]
    }

    comparison = calculate_buy_and_hold(
        bars,
        initial_equity=Decimal("1000"),
        costs=ZERO_COSTS,
        strategy_return=Decimal("0"),
        dividends_by_symbol=dividends,
    )

    assert comparison is not None
    # 10 unidades x 2 = 20 en cash.
    assert comparison.dividend_income == Decimal("20")
    assert comparison.final_equity == Decimal("1020")
    assert comparison.cumulative_return == Decimal("0.02")
