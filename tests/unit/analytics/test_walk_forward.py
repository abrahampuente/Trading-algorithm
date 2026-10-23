import math
import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.analytics.walk_forward import (
    PeriodEvaluation,
    SelectionMetric,
    WalkForwardAnalyzer,
    WalkForwardConfig,
    WalkForwardReport,
    add_months,
    annualized_return,
    build_windows,
    select_best,
    sharpe_ratio,
    walk_forward_to_markdown,
)
from algo_trading.backtesting import BacktestParameters, EquityPoint
from algo_trading.backtesting.sensitivity import ParameterGrid
from algo_trading.data.market_bar import MarketBar

START = datetime(2022, 1, 1, tzinfo=UTC)


# --- Ventanas ---------------------------------------------------------------


def test_add_months_clamps_to_month_end() -> None:
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2023, 11, 15), 3) == date(2024, 2, 15)
    assert add_months(date(2020, 1, 1), 36) == date(2023, 1, 1)


def test_default_windows_over_loaded_history() -> None:
    windows = build_windows(date(2020, 1, 1), date(2026, 10, 21), 36, 12)

    assert [(w.out_of_sample_start, w.out_of_sample_end) for w in windows] == [
        (date(2023, 1, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 10, 21)),
    ]
    assert windows[0].in_sample_start == date(2020, 1, 1)
    assert windows[0].in_sample_end == date(2022, 12, 31)
    assert windows[3].in_sample_start == date(2023, 1, 1)
    assert [w.is_partial for w in windows] == [False, False, False, True]
    assert [w.index for w in windows] == [1, 2, 3, 4]


def test_out_of_sample_windows_are_contiguous_and_do_not_overlap() -> None:
    windows = build_windows(date(2020, 1, 1), date(2026, 10, 21), 24, 6)

    for previous, current in zip(windows, windows[1:], strict=False):
        assert current.out_of_sample_start == previous.out_of_sample_end + timedelta(
            days=1
        )
        assert current.in_sample_end < current.out_of_sample_start


def test_period_too_short_gives_no_windows() -> None:
    assert build_windows(date(2024, 1, 1), date(2026, 12, 31), 36, 12) == ()


def test_step_shorter_than_oos_is_rejected() -> None:
    with pytest.raises(ValueError, match="step_months"):
        WalkForwardConfig(out_of_sample_months=12, step_months=6)


# --- Métricas ---------------------------------------------------------------


def test_annualized_return_compounds_over_calendar_days() -> None:
    value = annualized_return(Decimal("0.21"), date(2020, 1, 1), date(2021, 12, 31))

    assert value is not None
    expected = 1.21 ** (365.25 / 731) - 1
    assert math.isclose(float(value), expected, rel_tol=1e-12)


def test_annualized_return_is_none_after_total_loss() -> None:
    assert (
        annualized_return(Decimal("-1"), date(2020, 1, 1), date(2020, 12, 31)) is None
    )


def point(day: int, equity: str) -> EquityPoint:
    value = Decimal(equity)
    return EquityPoint(
        timestamp=START + timedelta(days=day),
        cash=value,
        market_value=Decimal("0"),
        equity=value,
        open_positions=0,
    )


def test_sharpe_ratio_of_daily_returns() -> None:
    curve = [point(1, "101"), point(2, "100"), point(3, "102")]

    value = sharpe_ratio(curve, Decimal("100"))

    returns = [0.01, 100 / 101 - 1, 0.02]
    mean = sum(returns) / 3
    std = math.sqrt(sum((r - mean) ** 2 for r in returns) / 2)
    assert value is not None
    assert math.isclose(float(value), mean / std * math.sqrt(252), rel_tol=1e-12)


def test_sharpe_ratio_is_none_without_volatility_or_data() -> None:
    assert sharpe_ratio([point(1, "100"), point(2, "100")], Decimal("100")) is None
    assert sharpe_ratio([point(1, "101")], Decimal("100")) is None


# --- Selección --------------------------------------------------------------


def evaluation(
    stop_loss: str,
    profit_factor: str | None,
    trades: int,
    sharpe: str | None = "1",
) -> PeriodEvaluation:
    return PeriodEvaluation(
        stop_loss_atr_multiple=Decimal(stop_loss),
        take_profit_atr_multiple=Decimal("2"),
        max_position_exposure=Decimal("0.10"),
        initial_equity=Decimal("100"),
        final_equity=Decimal("110"),
        cumulative_return=Decimal("0.1"),
        annualized_return=Decimal("0.05"),
        max_drawdown_pct=Decimal("0.02"),
        profit_factor=Decimal(profit_factor) if profit_factor is not None else None,
        sharpe_ratio=Decimal(sharpe) if sharpe is not None else None,
        win_rate=Decimal("0.5"),
        total_trades=trades,
    )


def test_selects_highest_profit_factor() -> None:
    candidates = [evaluation("1", "1.2", 20), evaluation("2", "1.6", 20)]

    best = select_best(candidates, SelectionMetric.PROFIT_FACTOR, min_trades=10)

    assert best.stop_loss_atr_multiple == Decimal("2")


def test_ignores_combinations_with_too_few_trades() -> None:
    candidates = [evaluation("1", "1.2", 20), evaluation("2", "5.0", 3)]

    best = select_best(candidates, SelectionMetric.PROFIT_FACTOR, min_trades=10)

    assert best.stop_loss_atr_multiple == Decimal("1")


def test_profit_factor_without_losses_counts_as_infinite() -> None:
    candidates = [evaluation("1", "3.0", 12), evaluation("2", None, 12)]

    best = select_best(candidates, SelectionMetric.PROFIT_FACTOR, min_trades=10)

    assert best.stop_loss_atr_multiple == Decimal("2")


def test_ties_keep_grid_order() -> None:
    candidates = [evaluation("1", "1.5", 12), evaluation("2", "1.5", 12)]

    best = select_best(candidates, SelectionMetric.PROFIT_FACTOR, min_trades=10)

    assert best.stop_loss_atr_multiple == Decimal("1")


def test_selects_by_sharpe_when_requested() -> None:
    candidates = [
        evaluation("1", "3.0", 12, sharpe="0.4"),
        evaluation("2", "1.1", 12, sharpe="0.9"),
    ]

    best = select_best(candidates, SelectionMetric.SHARPE, min_trades=10)

    assert best.stop_loss_atr_multiple == Decimal("2")


def test_falls_back_to_combinations_with_trades() -> None:
    candidates = [evaluation("1", None, 0), evaluation("2", "0.8", 2)]

    best = select_best(candidates, SelectionMetric.PROFIT_FACTOR, min_trades=10)

    assert best.stop_loss_atr_multiple == Decimal("2")


# --- Analizador -------------------------------------------------------------


def random_walk(symbol: str, seed: int, days: int) -> list[MarketBar]:
    generator = random.Random(seed)
    close = Decimal("100")
    bars = []

    for day in range(days):
        open_price = close + Decimal(generator.randint(-60, 80)) / 100
        close = max(
            Decimal("5"), open_price + Decimal(generator.randint(-150, 180)) / 100
        )
        open_price = max(Decimal("5"), open_price)
        high = max(open_price, close) + Decimal(generator.randint(0, 80)) / 100
        low = max(
            Decimal("1"),
            min(open_price, close) - Decimal(generator.randint(0, 80)) / 100,
        )
        bars.append(
            MarketBar(
                symbol=symbol,
                timestamp=START + timedelta(days=day),
                timeframe="1d",
                source="test",
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("1000"),
            )
        )

    return bars


def synthetic_bars() -> dict[str, list[MarketBar]]:
    return {symbol: random_walk(symbol, seed, 730) for seed, symbol in enumerate("ABC")}


CONFIG = WalkForwardConfig(
    in_sample_months=6,
    out_of_sample_months=3,
    min_trades=1,
    grid=ParameterGrid(
        stop_loss_atr_multiples=(Decimal("1"), Decimal("2")),
        max_position_exposures=(Decimal("0.10"), Decimal("0.20")),
    ),
)
PARAMETERS = BacktestParameters(initial_cash=Decimal("100000"), cost_profile="base")


@pytest.fixture(scope="module")
def report() -> WalkForwardReport:
    return WalkForwardAnalyzer(synthetic_bars()).run(PARAMETERS, CONFIG)


def test_analyzer_builds_one_result_per_window(report: WalkForwardReport) -> None:
    windows = build_windows(date(2022, 1, 1), date(2023, 12, 31), 6, 3)

    assert [result.window for result in report.windows] == list(windows)
    assert all(len(result.in_sample) == 4 for result in report.windows)
    assert all(result.selected in result.in_sample for result in report.windows)


def test_out_of_sample_windows_are_chained(report: WalkForwardReport) -> None:
    results = report.windows

    assert results[0].out_of_sample.initial_equity == PARAMETERS.initial_cash
    for previous, current in zip(results, results[1:], strict=False):
        assert (
            current.out_of_sample.initial_equity == previous.out_of_sample.final_equity
        )

    assert report.out_of_sample.final_equity == results[-1].out_of_sample.final_equity
    assert report.out_of_sample.cumulative_return == (
        report.out_of_sample.final_equity / PARAMETERS.initial_cash - 1
    )


def test_oos_curve_and_trades_stay_inside_oos_windows(
    report: WalkForwardReport,
) -> None:
    spans = [
        (r.window.out_of_sample_start, r.window.out_of_sample_end)
        for r in report.windows
    ]

    def inside(value: date) -> bool:
        return any(start <= value <= end for start, end in spans)

    dates = [p.timestamp.date() for p in report.out_of_sample_equity_curve]
    assert dates == sorted(set(dates))
    assert all(inside(value) for value in dates)
    assert report.out_of_sample_trades
    for trade in report.out_of_sample_trades:
        span = next(
            (start, end)
            for start, end in spans
            if start <= trade.entry_timestamp.date() <= end
        )
        assert trade.exit_timestamp.date() <= span[1]


def test_walk_forward_efficiency_formula(report: WalkForwardReport) -> None:
    oos = report.out_of_sample.annualized_return
    mean_is = report.mean_in_sample_annualized_return

    if oos is not None and mean_is is not None and mean_is > 0:
        assert report.walk_forward_efficiency == oos / mean_is
        assert report.is_robust == (report.walk_forward_efficiency > Decimal("0.5"))
    else:
        assert report.walk_forward_efficiency is None

    for result in report.windows:
        is_return = result.selected.annualized_return
        oos_return = result.out_of_sample.annualized_return
        if is_return is not None and oos_return is not None and is_return > 0:
            assert result.walk_forward_efficiency == oos_return / is_return


def test_in_sample_selection_does_not_see_future_data() -> None:
    original = synthetic_bars()
    window = build_windows(date(2022, 1, 1), date(2023, 12, 31), 6, 3)[0]
    # Datos posteriores al fin del IS completamente distintos.
    altered = {
        symbol: [
            bar
            if bar.timestamp.date() <= window.in_sample_end
            else bar.model_copy(
                update={
                    "open": bar.open * 3,
                    "high": bar.high * 3,
                    "low": bar.low * 3,
                    "close": bar.close * 3,
                }
            )
            for bar in bars
        ]
        for symbol, bars in original.items()
    }

    first = WalkForwardAnalyzer(original).run(PARAMETERS, CONFIG).windows[0]
    second = WalkForwardAnalyzer(altered).run(PARAMETERS, CONFIG).windows[0]

    assert first.in_sample == second.in_sample
    assert first.selected == second.selected


def test_period_too_short_raises() -> None:
    bars = {"A": random_walk("A", 1, 60)}

    with pytest.raises(ValueError, match="demasiado corto"):
        WalkForwardAnalyzer(bars).run(PARAMETERS, CONFIG)


def test_markdown_report_sections(report: WalkForwardReport) -> None:
    markdown = walk_forward_to_markdown(report)

    assert markdown.startswith("# Walk-Forward Optimization")
    for section in (
        "## Resultado fuera de muestra",
        "## Ventanas",
        "## Rejilla In-Sample por ventana",
        "## Notas de método",
        "**Eficiencia Walk-Forward (WFE)**",
        "Veredicto:",
    ):
        assert section in markdown
    assert markdown.count("### Ventana ") == len(report.windows)
    assert markdown.count("| ✓ |") == len(report.windows)


def test_report_includes_capital_usage_and_buy_and_hold(
    report: WalkForwardReport,
) -> None:
    oos = report.out_of_sample
    benchmark = report.buy_and_hold

    assert oos.time_in_market is not None
    assert 0 <= oos.time_in_market <= 1
    assert oos.average_exposure is not None
    assert 0 <= oos.average_exposure <= 1
    assert benchmark is not None
    assert benchmark.initial_equity == PARAMETERS.initial_cash
    assert benchmark.strategy_excess_return == (
        oos.cumulative_return - benchmark.cumulative_return
    )
    assert all(result.buy_and_hold_return is not None for result in report.windows)

    markdown = walk_forward_to_markdown(report)
    assert "## Uso de capital fuera de muestra" in markdown
    assert "Buy & Hold diversificado" in markdown


def test_take_profit_axis_is_evaluated_and_selected() -> None:
    config = CONFIG.model_copy(
        update={
            "grid": ParameterGrid(
                stop_loss_atr_multiples=(Decimal("1"),),
                take_profit_atr_multiples=(Decimal("2"), Decimal("3")),
                max_position_exposures=(Decimal("0.10"),),
            )
        }
    )

    result = WalkForwardAnalyzer(synthetic_bars()).run(PARAMETERS, config)

    for window in result.windows:
        assert [e.take_profit_atr_multiple for e in window.in_sample] == [
            Decimal("2"),
            Decimal("3"),
        ]
        assert window.selected.take_profit_atr_multiple in {Decimal("2"), Decimal("3")}
        assert window.out_of_sample.take_profit_atr_multiple == (
            window.selected.take_profit_atr_multiple
        )
