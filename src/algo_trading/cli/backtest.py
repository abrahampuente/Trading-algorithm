"""
Backtest sobre las barras ajustadas guardadas en MySQL.

Uso:

    algo-backtest
    algo-backtest --symbols AAPL MSFT --start 2021-01-01 --end 2025-12-31

Lee `market_bars_adjusted`, ejecuta la estrategia v1 y escribe el informe en
`<output-dir>/<name>.json` y `<output-dir>/<name>.md`.
"""

import argparse
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from algo_trading.analytics.report import BacktestReport, write_report
from algo_trading.backtesting.engine import BacktestParameters
from algo_trading.backtesting.run_service import (
    BacktestRunRequest,
    BacktestRunService,
    default_report_name,
)
from algo_trading.cli.ingest import (
    DEFAULT_ADJUSTMENT_VERSION,
    DEFAULT_START,
    DEFAULT_SYMBOLS,
    default_end_date,
)
from algo_trading.config.settings import PROJECT_ROOT
from algo_trading.data.providers.yfinance import YFINANCE_SOURCE
from algo_trading.execution.costs import CostModel
from algo_trading.portfolio.position_sizing import RiskParameters

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="algo-backtest",
        description="Ejecuta el backtest v1 sobre barras ajustadas de MySQL.",
    )
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        help="Fecha final inclusiva (por defecto: ayer en UTC).",
    )
    parser.add_argument(
        "--initial-cash",
        type=Decimal,
        default=None,
        help="Capital inicial (por defecto: initial_capital de la configuración).",
    )
    parser.add_argument("--gap-threshold-atr", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--source", default=YFINANCE_SOURCE)
    parser.add_argument("--adjustment-version", default=DEFAULT_ADJUSTMENT_VERSION)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--name",
        default=None,
        help="Nombre base de los ficheros (por defecto: símbolos y periodo).",
    )
    return parser.parse_args(argv)


def run_backtest_command(
    arguments: argparse.Namespace,
    session_factory: Callable[[], Session],
    default_initial_cash: Decimal,
    max_positions: int,
) -> tuple[BacktestReport, Path, Path]:
    symbols = tuple(symbol.upper() for symbol in arguments.symbols)
    end = arguments.end or default_end_date()
    costs = CostModel()
    parameters = BacktestParameters(
        initial_cash=arguments.initial_cash or default_initial_cash,
        risk=RiskParameters(
            max_positions=max_positions,
            commission_rate=costs.commission_rate,
        ),
        costs=costs,
        gap_threshold_atr=arguments.gap_threshold_atr,
    )
    request = BacktestRunRequest(
        symbols=symbols,
        start=arguments.start,
        end=end,
        parameters=parameters,
        source=arguments.source,
        adjustment_version=arguments.adjustment_version,
    )

    with session_factory() as session:
        report = BacktestRunService(session).run(request)

    name = arguments.name or default_report_name(symbols, arguments.start, end)
    json_path, markdown_path = write_report(report, arguments.output_dir, name)
    return report, json_path, markdown_path


def format_summary(report: BacktestReport, json_path: Path, markdown_path: Path) -> str:
    metrics = report.metrics
    trades = metrics.trades
    buy_and_hold = metrics.buy_and_hold

    def pct(value: Decimal | None) -> str:
        return "n/d" if value is None else f"{value * 100:.2f} %"

    lines = [
        f"Símbolos: {', '.join(report.symbols)}",
        f"Periodo: {report.start} → {report.end}",
        f"Equity final: {metrics.final_equity:,.2f}  "
        f"Retorno: {pct(metrics.cumulative_return)}  "
        f"MDD: {pct(metrics.max_drawdown.max_drawdown_pct)}",
        f"Trades: {trades.total_trades}  Win Rate: {pct(trades.win_rate)}  "
        f"Profit Factor: "
        f"{'n/d' if trades.profit_factor is None else f'{trades.profit_factor:.2f}'}",
    ]

    if buy_and_hold is not None:
        lines.append(
            f"Buy & Hold: {pct(buy_and_hold.cumulative_return)}  "
            f"MDD B&H: {pct(buy_and_hold.max_drawdown.max_drawdown_pct)}  "
            f"Exceso: {pct(buy_and_hold.strategy_excess_return)}"
        )

    lines.extend([f"Informe JSON: {json_path}", f"Informe Markdown: {markdown_path}"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)

    from algo_trading.config.settings import ApplicationSettings
    from algo_trading.persistence.database import SessionFactory

    settings = ApplicationSettings.from_yaml()
    report, json_path, markdown_path = run_backtest_command(
        arguments,
        session_factory=SessionFactory,
        default_initial_cash=settings.initial_capital,
        max_positions=settings.max_positions,
    )
    print(format_summary(report, json_path, markdown_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
