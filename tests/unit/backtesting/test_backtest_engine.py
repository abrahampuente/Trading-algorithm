from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.backtesting import (
    BacktestEngine,
    BacktestParameters,
    BacktestResult,
)
from algo_trading.data.market_bar import MarketBar
from algo_trading.execution import CostModel, ExitReason
from algo_trading.portfolio import RejectionReason, RiskParameters

START = datetime(2024, 1, 1, tzinfo=UTC)
INITIAL_CASH = Decimal("100000")

# Con la serie base, la señal se emite en el índice 20 con:
# ATR = (19 * 2 + 9.1) / 20 = 2.355 y umbral anti-gap 0.5 * ATR = 1.1775.
SIGNAL_ATR = Decimal("2.355")
ENTRY_OPEN = Decimal("110")
ENTRY_EXECUTED = Decimal("110.055")  # 110 * 1.0005
STOP_LOSS = Decimal("107.700")  # 110.055 - 2.355
TAKE_PROFIT = Decimal("114.765")  # 110.055 + 2 * 2.355
QUANTITY = 90  # floor(10000 / 110.055): limitado por exposición


def bar(
    index: int,
    open_price: str,
    high: str,
    low: str,
    close: str,
    symbol: str = "AAA",
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        timestamp=START + timedelta(days=index),
        timeframe="1d",
        source="test",
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1000"),
    )


def signal_series(symbol: str = "AAA", breakout_close: str = "110") -> list[MarketBar]:
    """20 barras de tendencia suave y ruptura alcista en el índice 20."""
    bars = []

    for index in range(20):
        close = Decimal("100") + Decimal("0.1") * index
        bars.append(
            bar(index, str(close), str(close + 1), str(close - 1), str(close), symbol)
        )

    breakout = Decimal(breakout_close)
    bars.append(
        bar(
            20,
            str(breakout),
            str(breakout + 1),
            str(breakout - 1),
            str(breakout),
            symbol,
        )
    )
    return bars


def flat_bar(index: int, symbol: str = "AAA") -> MarketBar:
    return bar(index, "110", "110.5", "109.5", "110", symbol)


def run_single(bars: list[MarketBar]) -> BacktestResult:
    return BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(
        {"AAA": bars}
    )


def test_enters_at_next_open_with_slippage_and_frozen_atr_levels() -> None:
    bars = [*signal_series(), flat_bar(21), flat_bar(22)]

    result = run_single(bars)

    trade = result.trades[0]
    assert trade.signal_timestamp == bars[20].timestamp
    assert trade.entry_timestamp == bars[21].timestamp
    assert trade.entry_theoretical_price == ENTRY_OPEN
    assert trade.entry_executed_price == ENTRY_EXECUTED
    assert trade.entry_atr == SIGNAL_ATR
    assert trade.stop_loss == STOP_LOSS
    assert trade.take_profit == TAKE_PROFIT
    assert trade.quantity == QUANTITY


def test_signal_on_last_bar_produces_no_entry() -> None:
    result = run_single(signal_series())

    assert result.trades == ()
    assert result.gap_filtered == ()


# --- Escudo anti-gap ---------------------------------------------------------


def test_gap_above_threshold_cancels_entry_as_gap_filtered() -> None:
    bars = [*signal_series(), bar(21, "111.2", "112", "111", "111.5"), flat_bar(22)]

    result = run_single(bars)

    assert result.trades == ()
    assert len(result.gap_filtered) == 1
    filtered = result.gap_filtered[0]
    assert filtered.reason == "GAP_FILTERED"
    assert filtered.session_timestamp == bars[21].timestamp
    assert filtered.signal.signal_timestamp == bars[20].timestamp
    assert filtered.entry_open == Decimal("111.2")
    assert filtered.gap == Decimal("1.2")
    assert filtered.max_allowed_gap == Decimal("1.1775")


def test_gap_equal_to_threshold_is_allowed() -> None:
    bars = [
        *signal_series(),
        bar(21, "111.1775", "111.5", "110.5", "111"),
        bar(22, "111", "111.5", "110.5", "111"),
    ]

    result = run_single(bars)

    assert result.gap_filtered == ()
    assert result.trades[0].entry_theoretical_price == Decimal("111.1775")


def test_gap_down_is_never_filtered() -> None:
    bars = [*signal_series(), bar(21, "109", "109.5", "108.5", "109"), flat_bar(22)]

    result = run_single(bars)

    assert result.gap_filtered == ()
    assert len(result.trades) == 1


def test_configurable_gap_threshold() -> None:
    bars = [*signal_series(), bar(21, "111.2", "112", "111", "111.5"), flat_bar(22)]

    result = BacktestEngine(
        BacktestParameters(initial_cash=INITIAL_CASH, gap_threshold_atr=Decimal("1"))
    ).run({"AAA": bars})

    assert result.gap_filtered == ()
    assert len(result.trades) == 1


def test_gap_filtered_signal_does_not_consume_a_position_slot() -> None:
    gapped = [
        *signal_series("AAA", breakout_close="115"),
        bar(21, "120", "121", "119", "120", "AAA"),
    ]
    normal = [*signal_series("BBB"), flat_bar(21, "BBB")]

    result = BacktestEngine(
        BacktestParameters(
            initial_cash=INITIAL_CASH,
            risk=RiskParameters(
                max_positions=1,
                commission_rate=Decimal("0.0005"),
            ),
        )
    ).run({"AAA": gapped, "BBB": normal})

    assert [entry.signal.symbol for entry in result.gap_filtered] == ["AAA"]
    assert [trade.symbol for trade in result.trades] == ["BBB"]
    assert result.rejected == ()


# --- Salidas y prioridad del SL ---------------------------------------------


def test_take_profit_exit() -> None:
    bars = [*signal_series(), flat_bar(21), bar(22, "111", "115", "110.5", "114")]

    trade = run_single(bars).trades[0]

    assert trade.exit_reason is ExitReason.TAKE_PROFIT
    assert trade.exit_theoretical_price == TAKE_PROFIT
    assert trade.exit_timestamp == bars[22].timestamp
    assert trade.sessions_since_entry == 1


def test_stop_loss_exit() -> None:
    bars = [*signal_series(), flat_bar(21), bar(22, "110", "111", "107", "108")]

    trade = run_single(bars).trades[0]

    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_theoretical_price == STOP_LOSS


def test_stop_loss_wins_when_bar_reaches_stop_and_target() -> None:
    bars = [*signal_series(), flat_bar(21), bar(22, "110", "115", "107", "114")]

    trade = run_single(bars).trades[0]

    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_theoretical_price == STOP_LOSS
    assert trade.net_pnl < 0


def test_stop_loss_wins_on_entry_bar_reaching_both_levels() -> None:
    bars = [*signal_series(), bar(21, "110", "115", "107", "114"), flat_bar(22)]

    trade = run_single(bars).trades[0]

    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.entry_timestamp == trade.exit_timestamp == bars[21].timestamp
    assert trade.sessions_since_entry == 0


def test_stop_loss_wins_when_open_gaps_above_target_then_falls() -> None:
    bars = [*signal_series(), flat_bar(21), bar(22, "116", "117", "107", "108")]

    trade = run_single(bars).trades[0]

    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_theoretical_price == STOP_LOSS


def test_gap_below_stop_executes_at_worse_open() -> None:
    bars = [*signal_series(), flat_bar(21), bar(22, "106", "107", "105", "106")]

    trade = run_single(bars).trades[0]

    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_theoretical_price == Decimal("106")
    assert trade.exit_theoretical_price < trade.stop_loss


def test_time_exit_at_open_after_twenty_sessions() -> None:
    bars = [
        *signal_series(),
        *(flat_bar(index) for index in range(21, 41)),
        bar(41, "110.2", "110.5", "109.5", "110"),
        flat_bar(42),
    ]

    result = run_single(bars)

    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.TIME_EXIT
    assert trade.exit_timestamp == bars[41].timestamp
    assert trade.exit_theoretical_price == Decimal("110.2")
    assert trade.sessions_since_entry == 20
    assert result.equity_curve[40].open_positions == 1
    assert result.equity_curve[41].open_positions == 0


def test_open_position_is_closed_at_end_of_data() -> None:
    bars = [*signal_series(), flat_bar(21), bar(22, "110", "111", "109", "110.5")]

    trade = run_single(bars).trades[0]

    assert trade.exit_reason is ExitReason.END_OF_DATA
    assert trade.exit_theoretical_price == Decimal("110.5")
    assert trade.exit_timestamp == bars[22].timestamp


# --- Costes y contabilidad --------------------------------------------------


def test_costs_are_applied_and_cash_is_conserved() -> None:
    bars = [*signal_series(), flat_bar(21), bar(22, "111", "115", "110.5", "114")]

    result = run_single(bars)

    trade = result.trades[0]
    assert trade.exit_executed_price == TAKE_PROFIT * Decimal("0.9995")
    assert trade.entry_commission == ENTRY_EXECUTED * QUANTITY * Decimal("0.0005")
    assert trade.exit_commission == (
        trade.exit_executed_price * QUANTITY * Decimal("0.0005")
    )
    assert trade.gross_pnl == (TAKE_PROFIT - ENTRY_OPEN) * QUANTITY
    assert trade.net_pnl == trade.gross_pnl - trade.total_costs
    assert result.final_cash == INITIAL_CASH + trade.net_pnl
    assert result.equity_curve[-1].equity == result.final_cash


def test_zero_cost_profile_makes_gross_equal_net() -> None:
    bars = [*signal_series(), flat_bar(21), bar(22, "111", "115", "110.5", "114")]
    zero = Decimal("0")

    result = BacktestEngine(
        BacktestParameters(
            initial_cash=INITIAL_CASH,
            costs=CostModel(commission_rate=zero, slippage_rate=zero),
            risk=RiskParameters(commission_rate=zero),
        )
    ).run({"AAA": bars})

    trade = result.trades[0]
    assert trade.total_costs == 0
    assert trade.net_pnl == trade.gross_pnl


def test_rejects_inconsistent_commission_between_risk_and_costs() -> None:
    with pytest.raises(ValueError, match="commission_rate"):
        BacktestParameters(
            initial_cash=INITIAL_CASH,
            risk=RiskParameters(commission_rate=Decimal("0")),
        )


# --- Portfolio --------------------------------------------------------------


def test_simultaneous_signals_respect_five_position_limit_and_momentum() -> None:
    breakouts = {
        "AAA": "110",
        "BBB": "111",
        "CCC": "112",
        "DDD": "113",
        "EEE": "114",
        "FFF": "115",
    }
    series = {
        symbol: [*signal_series(symbol, close), flat_bar(21, symbol)]
        for symbol, close in breakouts.items()
    }
    # Apertura igual al cierre de la señal para no activar el anti-gap.
    for symbol, close in breakouts.items():
        series[symbol][21] = bar(21, close, close, close, close, symbol)

    result = BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(series)

    assert sorted(trade.symbol for trade in result.trades) == [
        "BBB",
        "CCC",
        "DDD",
        "EEE",
        "FFF",
    ]
    assert [(r.candidate.symbol, r.reason) for r in result.rejected] == [
        ("AAA", RejectionReason.MAX_POSITIONS_REACHED)
    ]
    assert result.equity_curve[21].open_positions == 5


def test_rejects_series_with_mismatched_symbol() -> None:
    with pytest.raises(ValueError, match="BBB"):
        BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(
            {"BBB": signal_series("AAA")}
        )
