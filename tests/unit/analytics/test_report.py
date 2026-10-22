import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from algo_trading.analytics import (
    BacktestReport,
    build_report,
    calculate_performance_metrics,
    report_to_json,
    report_to_markdown,
    write_report,
)
from algo_trading.backtesting import BacktestEngine, BacktestParameters
from algo_trading.data.market_bar import MarketBar

START = datetime(2024, 1, 1, tzinfo=UTC)
INITIAL_CASH = Decimal("100000")


def bar(index: int, open_price: str, high: str, low: str, close: str) -> MarketBar:
    return MarketBar(
        symbol="AAA",
        timestamp=START + timedelta(days=index),
        timeframe="1d",
        source="test",
        open=Decimal(open_price),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1000"),
    )


def take_profit_scenario() -> dict[str, list[MarketBar]]:
    """Señal en el índice 20, entrada en 21 y TP en 22."""
    bars = []

    for index in range(20):
        close = Decimal("100") + Decimal("0.1") * index
        bars.append(bar(index, str(close), str(close + 1), str(close - 1), str(close)))

    bars.extend(
        [
            bar(20, "110", "111", "109", "110"),
            bar(21, "110", "110.5", "109.5", "110"),
            bar(22, "111", "115", "110.5", "114"),
        ]
    )
    return {"AAA": bars}


def build_scenario_report() -> BacktestReport:
    bars = take_profit_scenario()
    result = BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(bars)
    return build_report(result, bars)


def test_performance_metrics_from_backtest() -> None:
    bars = take_profit_scenario()
    result = BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(bars)

    metrics = calculate_performance_metrics(result, bars)

    trade = result.trades[0]
    assert metrics.final_equity == result.final_cash
    assert metrics.net_profit == trade.net_pnl
    assert metrics.cumulative_return == result.final_cash / INITIAL_CASH - 1
    assert metrics.trades.total_trades == 1
    assert metrics.trades.win_rate == Decimal("1")
    assert metrics.buy_and_hold is not None
    assert metrics.buy_and_hold.strategy_excess_return == (
        metrics.cumulative_return - metrics.buy_and_hold.cumulative_return
    )


def test_metrics_without_bars_skip_buy_and_hold() -> None:
    bars = take_profit_scenario()
    result = BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(bars)

    assert calculate_performance_metrics(result).buy_and_hold is None


def test_json_export_preserves_decimals_and_round_trips() -> None:
    report = build_scenario_report()

    exported = report_to_json(report)
    payload = json.loads(exported)

    assert payload["report_version"] == "backtest-report-v1"
    assert payload["symbols"] == ["AAA"]
    assert payload["parameters"]["initial_cash"] == "100000"
    assert payload["parameters"]["costs"]["commission_rate"] == "0.0005"
    assert payload["metrics"]["trades"]["total_trades"] == 1
    assert payload["trades"][0]["exit_reason"] == "TAKE_PROFIT"
    assert isinstance(payload["metrics"]["cumulative_return"], str)
    assert BacktestReport.model_validate_json(exported) == report


def test_report_is_deterministic() -> None:
    first = build_scenario_report()
    second = build_scenario_report()

    assert report_to_json(first) == report_to_json(second)
    assert report_to_markdown(first) == report_to_markdown(second)


def test_markdown_summary_contains_parameters_and_results() -> None:
    report = build_scenario_report()

    markdown = report_to_markdown(report)

    assert markdown.startswith("# Informe de backtest")
    for section in ("## Parámetros", "## Resultados", "## Operaciones"):
        assert section in markdown
    assert "| Capital inicial | 100,000.00 |" in markdown
    assert "| Riesgo por operación | 0.50 % |" in markdown
    assert "| Comisión | 0.05 % |" in markdown
    assert "| Win Rate | 100.00 % |" in markdown
    assert "| Profit Factor | n/d |" in markdown
    assert "Buy & Hold" in markdown
    assert "TAKE_PROFIT 1" in markdown
    assert "| AAA | 2024-01-22 | 2024-01-23 | 90 |" in markdown


def test_markdown_without_trades() -> None:
    bars = {"AAA": take_profit_scenario()["AAA"][:15]}
    result = BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(bars)

    markdown = report_to_markdown(build_report(result, bars))

    assert "Sin operaciones." in markdown
    assert "| Win Rate | n/d |" in markdown


def test_write_report_creates_json_and_markdown(tmp_path: Path) -> None:
    report = build_scenario_report()

    json_path, markdown_path = write_report(report, tmp_path / "reports", "run-1")

    assert json_path.name == "run-1.json"
    assert markdown_path.name == "run-1.md"
    assert BacktestReport.model_validate_json(
        json_path.read_text(encoding="utf-8")
    ) == (report)
    assert markdown_path.read_text(encoding="utf-8") == report_to_markdown(report)
