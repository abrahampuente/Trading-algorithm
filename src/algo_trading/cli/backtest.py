"""
Backtest sobre las barras ajustadas guardadas en MySQL.

Uso:

    algo-backtest
    algo-backtest --symbols AAPL MSFT --start 2021-01-01 --end 2025-12-31
    algo-backtest --list-runs 10

Lee `market_bars_adjusted` y los dividendos, valida las series contra el
calendario de mercado, ejecuta la estrategia v1, guarda la ejecución en
MySQL (`backtest_runs`, `backtest_trades`, `backtest_equity_snapshots`) y
escribe el informe en `<output-dir>/<name>.json` y `<output-dir>/<name>.md`.
"""

import argparse
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from algo_trading.analytics.report import write_report
from algo_trading.backtesting.engine import BacktestParameters
from algo_trading.backtesting.run_service import (
    BacktestRunOutcome,
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
from algo_trading.data.market_calendar import DEFAULT_EXCHANGE, TradingCalendar
from algo_trading.data.providers.yfinance import YFINANCE_SOURCE
from algo_trading.execution.costs import CostModel
from algo_trading.persistence.repositories.backtest_run_repository import (
    BacktestRunRepository,
    BacktestRunSummary,
)
from algo_trading.portfolio.position_sizing import RiskParameters

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports"
NO_CALENDAR = "none"


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
    parser.add_argument(
        "--calendar",
        default=DEFAULT_EXCHANGE,
        help="Calendario de pandas-market-calendars (NYSE, NASDAQ) o 'none'.",
    )
    parser.add_argument(
        "--no-dividends",
        action="store_true",
        help="No acreditar dividendos (estrategia ni Buy & Hold).",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="No guardar la ejecución en MySQL.",
    )
    parser.add_argument(
        "--list-runs",
        type=int,
        metavar="N",
        default=None,
        help="Muestra las N ejecuciones más recientes guardadas y termina.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--name",
        default=None,
        help="Nombre base de los ficheros (por defecto: símbolos y periodo).",
    )
    return parser.parse_args(argv)


def build_calendar(name: str) -> TradingCalendar | None:
    if name.lower() == NO_CALENDAR:
        return None

    from algo_trading.data.market_calendar import ExchangeCalendar

    return ExchangeCalendar(name.upper())


def run_backtest_command(
    arguments: argparse.Namespace,
    session_factory: Callable[[], Session],
    default_initial_cash: Decimal,
    max_positions: int,
    calendar: TradingCalendar | None,
) -> tuple[BacktestRunOutcome, Path, Path]:
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
        include_dividends=not arguments.no_dividends,
    )

    with session_factory() as session:
        outcome = BacktestRunService(session, calendar=calendar).run(
            request,
            persist=not arguments.no_persist,
        )

    name = arguments.name or default_report_name(symbols, arguments.start, end)
    json_path, markdown_path = write_report(outcome.report, arguments.output_dir, name)
    return outcome, json_path, markdown_path


def format_summary(
    outcome: BacktestRunOutcome,
    json_path: Path,
    markdown_path: Path,
) -> str:
    report = outcome.report
    metrics = report.metrics
    trades = metrics.trades
    buy_and_hold = metrics.buy_and_hold

    lines = [
        f"Ejecución: {outcome.run_id or 'no guardada'}",
        f"Símbolos: {', '.join(report.symbols)}",
        f"Periodo: {report.start} → {report.end}  "
        f"Calendario: {report.trading_calendar or 'ninguno'}",
        f"Equity final: {metrics.final_equity:,.2f}  "
        f"Retorno: {_pct(metrics.cumulative_return)}  "
        f"MDD: {_pct(metrics.max_drawdown.max_drawdown_pct)}  "
        f"Dividendos: {metrics.dividend_income:,.2f}",
        f"Trades: {trades.total_trades}  Win Rate: {_pct(trades.win_rate)}  "
        f"Profit Factor: {_ratio(trades.profit_factor)}",
    ]

    if buy_and_hold is not None:
        lines.append(
            f"Buy & Hold: {_pct(buy_and_hold.cumulative_return)}  "
            f"MDD B&H: {_pct(buy_and_hold.max_drawdown.max_drawdown_pct)}  "
            f"Dividendos B&H: {buy_and_hold.dividend_income:,.2f}  "
            f"Exceso: {_pct(buy_and_hold.strategy_excess_return)}"
        )

    lines.extend([f"Informe JSON: {json_path}", f"Informe Markdown: {markdown_path}"])
    return "\n".join(lines)


def format_runs(runs: Sequence[BacktestRunSummary]) -> str:
    if not runs:
        return "No hay ejecuciones guardadas."

    lines = [
        f"{'run_id':<36} {'creado (UTC)':<19} {'periodo':<23} {'gap':>4} "
        f"{'retorno':>9} {'MDD':>8} {'trades':>6} {'PF':>5} {'B&H':>9}"
    ]

    for run in runs:
        gap = run.parameters.get("gap_threshold_atr", "-")
        lines.append(
            f"{run.run_id:<36} {run.created_at:%Y-%m-%d %H:%M:%S} "
            f"{run.start_date} → {run.end_date} {gap!s:>4} "
            f"{_pct(run.cumulative_return):>9} {_pct(run.max_drawdown_pct):>8} "
            f"{run.total_trades:>6} {_ratio(run.profit_factor):>5} "
            f"{_pct(run.buy_and_hold_return):>9}"
        )

    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)

    from algo_trading.config.settings import ApplicationSettings
    from algo_trading.persistence.database import SessionFactory

    if arguments.list_runs is not None:
        with SessionFactory() as session:
            runs = BacktestRunRepository(session).list_summaries(arguments.list_runs)
        print(format_runs(runs))
        return 0

    settings = ApplicationSettings.from_yaml()
    outcome, json_path, markdown_path = run_backtest_command(
        arguments,
        session_factory=SessionFactory,
        default_initial_cash=settings.initial_capital,
        max_positions=settings.max_positions,
        calendar=build_calendar(arguments.calendar),
    )
    print(format_summary(outcome, json_path, markdown_path))
    return 0


def _pct(value: Decimal | None) -> str:
    return "n/d" if value is None else f"{value * 100:.2f} %"


def _ratio(value: Decimal | None) -> str:
    return "n/d" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
