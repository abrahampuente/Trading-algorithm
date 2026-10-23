"""
Backtest sobre las barras ajustadas guardadas en MySQL.

Uso:

    algo-backtest
    algo-backtest --symbols AAPL MSFT --start 2021-01-01 --end 2025-12-31
    algo-backtest --cost-profile conservador --stop-loss-atr 1.5
    algo-backtest --sweep
    algo-backtest --sweep --sl-multiples 1 2 --exposures 0.10 0.20
    algo-backtest --walk-forward --is-months 36 --oos-months 12
    algo-backtest --walk-forward --universe diversified --tp-multiples 2 2.5 3
    algo-backtest --walk-forward --universe diversified --positions-grid 5 8 10 \
        --risk-grid 0.005 0.01 0.015 --report-tag portfolio_scaling
    algo-backtest --list-runs 10
    algo-backtest --use-sector-relative-strength --vix-filter-threshold 25
    algo-backtest --walk-forward --universe diversified --sector-filter-grid off on \
        --report-tag sector_filter

Lee `market_bars_adjusted` y los dividendos, valida las series contra el
calendario de mercado, ejecuta la estrategia v1, guarda la ejecución en
MySQL (`backtest_runs`, `backtest_trades`, `backtest_equity_snapshots`) y
escribe el informe en `<output-dir>/<name>.json` y `<output-dir>/<name>.md`.

Con `--sweep` ejecuta un backtest por combinación de multiplicador de stop
loss y exposición máxima por posición, guarda todas las ejecuciones con el
mismo `sweep_id` y escribe una tabla comparativa.

Con `--walk-forward` optimiza la misma rejilla en ventanas In-Sample
rodantes, aplica la combinación elegida a la ventana Out-of-Sample siguiente
y escribe `reports/walk_forward_*.md` con la curva OOS encadenada y la
Eficiencia Walk-Forward (WFE).

Con `--use-sector-relative-strength` y/o `--vix-filter-threshold`, las
señales v1 pasan por los filtros de régimen v2 (`signals/`) con las series de
referencia de `signals.reference_universe`. Sin esas opciones, el backtest es
exactamente el de la v1. En walk-forward, `--sector-filter-grid off on` añade
el filtro sectorial como eje de la rejilla: cada IS decide si usarlo.
"""

import argparse
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from algo_trading.analytics.report import write_report
from algo_trading.analytics.walk_forward import (
    SelectionMetric,
    WalkForwardAnalyzer,
    WalkForwardConfig,
    WalkForwardReport,
    write_walk_forward_report,
)
from algo_trading.backtesting.engine import BacktestParameters
from algo_trading.backtesting.run_service import (
    BacktestRunOutcome,
    BacktestRunRequest,
    BacktestRunService,
    default_report_name,
)
from algo_trading.backtesting.sensitivity import (
    DEFAULT_MAX_POSITION_EXPOSURES,
    DEFAULT_STOP_LOSS_ATR_MULTIPLES,
    DEFAULT_TAKE_PROFIT_ATR_MULTIPLES,
    ParameterGrid,
    SensitivityAnalysisService,
    SensitivityReport,
    sensitivity_table,
    write_sensitivity_report,
)
from algo_trading.cli.ingest import (
    DEFAULT_ADJUSTMENT_VERSION,
    DEFAULT_START,
    default_end_date,
)
from algo_trading.config.settings import (
    DEFAULT_COST_PROFILE,
    PROJECT_ROOT,
    ApplicationSettings,
)
from algo_trading.data.market_calendar import DEFAULT_EXCHANGE, TradingCalendar
from algo_trading.data.providers.yfinance import YFINANCE_SOURCE
from algo_trading.execution.costs import CostModel
from algo_trading.persistence.repositories.backtest_run_repository import (
    BacktestRunRepository,
    BacktestRunSummary,
)
from algo_trading.portfolio.position_sizing import RiskParameters
from algo_trading.signals import (
    MOMENTUM_BREAKOUT_V1,
    CompositeSignalGenerator,
    MomentumBreakoutGenerator,
    SectorRotationFilter,
    SignalFilter,
    VixRegimeFilter,
)

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "reports"
NO_CALENDAR = "none"
SECTOR_FILTER_OFF = "off"
SECTOR_FILTER_ON = "on"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="algo-backtest",
        description="Ejecuta el backtest v1 sobre barras ajustadas de MySQL.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--symbols", nargs="+", default=None)
    selection.add_argument(
        "--universe",
        default=None,
        help="Universo de la configuración (por defecto: default_universe).",
    )
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
    parser.add_argument(
        "--cost-profile",
        default=DEFAULT_COST_PROFILE,
        help="Perfil de costes de la configuración: optimista, base o conservador.",
    )
    parser.add_argument("--gap-threshold-atr", type=Decimal, default=Decimal("0.5"))
    parser.add_argument(
        "--stop-loss-atr",
        type=Decimal,
        default=None,
        help="Multiplicador de ATR para el stop loss (por defecto: configuración).",
    )
    parser.add_argument(
        "--take-profit-atr",
        type=Decimal,
        default=None,
        help="Multiplicador de ATR para el take profit (por defecto: configuración).",
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=None,
        help="Posiciones simultáneas máximas (por defecto: max_positions de la "
        "configuración).",
    )
    parser.add_argument(
        "--risk-per-trade",
        type=Decimal,
        default=None,
        help="Riesgo por operación sobre el equity (por defecto: configuración).",
    )
    parser.add_argument(
        "--max-position-exposure",
        type=Decimal,
        default=None,
        help="Exposición nominal máxima por posición (por defecto: configuración).",
    )
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
        "--sweep",
        action="store_true",
        help="Barrido de --sl-multiples x --exposures en lugar de un único backtest.",
    )
    parser.add_argument(
        "--sl-multiples",
        nargs="+",
        type=Decimal,
        default=list(DEFAULT_STOP_LOSS_ATR_MULTIPLES),
        help="Multiplicadores de stop loss del barrido (por defecto: 1.0 1.5 2.0).",
    )
    parser.add_argument(
        "--tp-multiples",
        nargs="+",
        type=Decimal,
        default=list(DEFAULT_TAKE_PROFIT_ATR_MULTIPLES),
        help="Multiplicadores de take profit de la rejilla (por defecto: 2.0).",
    )
    parser.add_argument(
        "--exposures",
        nargs="+",
        type=Decimal,
        default=list(DEFAULT_MAX_POSITION_EXPOSURES),
        help="Exposiciones máximas del barrido (por defecto: 0.10 0.15 0.20).",
    )
    parser.add_argument(
        "--positions-grid",
        nargs="+",
        type=int,
        default=None,
        help="Posiciones simultáneas de la rejilla (por defecto: --max-positions).",
    )
    parser.add_argument(
        "--risk-grid",
        nargs="+",
        type=Decimal,
        default=None,
        help="Riesgos por operación de la rejilla (por defecto: --risk-per-trade).",
    )
    parser.add_argument(
        "--report-tag",
        default=None,
        help="Etiqueta tras el prefijo del informe (p. ej. portfolio_scaling).",
    )
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Walk-forward: optimiza la rejilla en IS y la aplica en OOS.",
    )
    parser.add_argument(
        "--is-months",
        type=int,
        default=36,
        help="Meses de cada ventana In-Sample (por defecto: 36).",
    )
    parser.add_argument(
        "--oos-months",
        type=int,
        default=12,
        help="Meses de cada ventana Out-of-Sample (por defecto: 12).",
    )
    parser.add_argument(
        "--wf-step-months",
        type=int,
        default=None,
        help="Desplazamiento entre ventanas (por defecto: --oos-months).",
    )
    parser.add_argument(
        "--wf-metric",
        choices=[metric.value for metric in SelectionMetric],
        default=SelectionMetric.PROFIT_FACTOR.value,
        help="Métrica para elegir la combinación IS (por defecto: profit_factor).",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=10,
        help="Trades IS mínimos para que una combinación sea elegible.",
    )
    parser.add_argument(
        "--list-runs",
        type=int,
        metavar="N",
        default=None,
        help="Muestra las N ejecuciones más recientes guardadas y termina.",
    )
    parser.add_argument(
        "--sweep-id",
        default=None,
        help="Con --list-runs, muestra sólo las ejecuciones de ese barrido.",
    )
    parser.add_argument(
        "--use-sector-relative-strength",
        action="store_true",
        help="Filtro v2: sólo señales de los sectores con mayor fuerza relativa "
        "frente al benchmark (configuración signals).",
    )
    parser.add_argument(
        "--sector-filter-grid",
        nargs="+",
        choices=[SECTOR_FILTER_OFF, SECTOR_FILTER_ON],
        default=None,
        help="Walk-forward: eje del filtro sectorial en la rejilla (p. ej. "
        "'off on'; por defecto sólo off, la señal v1).",
    )
    parser.add_argument(
        "--vix-filter-threshold",
        type=Decimal,
        default=None,
        help="Filtro v2: sólo señales con cierre del VIX <= umbral (p. ej. 25).",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--name",
        default=None,
        help="Nombre base de los ficheros (por defecto: símbolos y periodo).",
    )
    arguments = parser.parse_args(argv)

    if uses_signal_filters(arguments) and (arguments.sweep or arguments.walk_forward):
        parser.error(
            "Los filtros de régimen v2 sólo están disponibles en un backtest "
            "individual (sin --sweep ni --walk-forward); en walk-forward, use "
            "--sector-filter-grid"
        )

    if arguments.sector_filter_grid and not arguments.walk_forward:
        parser.error("--sector-filter-grid sólo se usa con --walk-forward")

    return arguments


def sector_filter_grid(arguments: argparse.Namespace) -> tuple[bool, ...]:
    """Valores del eje `use_sector_filter`; sin opción, sólo la señal v1."""
    values = arguments.sector_filter_grid or [SECTOR_FILTER_OFF]
    return tuple(value == SECTOR_FILTER_ON for value in values)


def needs_reference_series(arguments: argparse.Namespace) -> bool:
    return uses_signal_filters(arguments) or True in sector_filter_grid(arguments)


def build_sector_filter(settings: ApplicationSettings) -> SectorRotationFilter:
    """Filtro de fuerza relativa sectorial con la configuración `signals`."""
    signals = settings.signals
    return SectorRotationFilter(
        benchmark=signals.benchmark,
        sector_map=signals.sector_map,
        sector_etfs=signals.sector_etfs,
        lookback=signals.relative_strength_lookback,
        top_k=signals.sector_top_k,
        allow_unmapped=signals.sector_allow_unmapped,
    )


def uses_signal_filters(arguments: argparse.Namespace) -> bool:
    return bool(
        arguments.use_sector_relative_strength
        or arguments.vix_filter_threshold is not None
    )


def build_signal_generator(
    arguments: argparse.Namespace,
    settings: ApplicationSettings,
) -> CompositeSignalGenerator | None:
    """Señal v1 con los filtros de régimen pedidos, o None sin filtros (v1)."""
    signals = settings.signals
    filters: list[SignalFilter] = []
    labels: list[str] = []

    if arguments.use_sector_relative_strength:
        filters.append(build_sector_filter(settings))
        labels.append("sector_rs")

    if arguments.vix_filter_threshold is not None:
        filters.append(
            VixRegimeFilter(
                symbol=signals.vix_symbol,
                max_level=arguments.vix_filter_threshold,
            )
        )
        labels.append("vix")

    if not filters:
        return None

    return CompositeSignalGenerator(
        MomentumBreakoutGenerator(),
        filters=filters,
        strategy_id="+".join([MOMENTUM_BREAKOUT_V1, *labels]),
    )


def signal_filter_tag(arguments: argparse.Namespace) -> str | None:
    """Etiqueta de informe para los filtros activos (p. ej. `sector-rs_vix25`)."""
    parts: list[str] = []

    if arguments.use_sector_relative_strength:
        parts.append("sector-rs")

    if arguments.vix_filter_threshold is not None:
        parts.append(f"vix{arguments.vix_filter_threshold.normalize():f}")

    return "_".join(parts) or None


def build_calendar(name: str) -> TradingCalendar | None:
    if name.lower() == NO_CALENDAR:
        return None

    from algo_trading.data.market_calendar import ExchangeCalendar

    return ExchangeCalendar(name.upper())


def build_request(
    arguments: argparse.Namespace,
    settings: ApplicationSettings,
) -> BacktestRunRequest:
    profile_name = arguments.cost_profile.lower()
    profile = settings.cost_profile(profile_name)
    strategy = settings.strategy
    costs = CostModel(
        commission_rate=profile.commission_rate,
        slippage_rate=profile.slippage_rate,
    )
    parameters = BacktestParameters(
        initial_cash=arguments.initial_cash or settings.initial_capital,
        cost_profile=profile_name,
        risk=RiskParameters(
            max_positions=arguments.max_positions or settings.max_positions,
            risk_per_trade=_or_default(
                arguments.risk_per_trade, strategy.risk_per_trade
            ),
            max_position_exposure=_or_default(
                arguments.max_position_exposure, strategy.max_position_exposure
            ),
            stop_loss_atr_multiple=_or_default(
                arguments.stop_loss_atr, strategy.stop_loss_atr_multiple
            ),
            take_profit_atr_multiple=_or_default(
                arguments.take_profit_atr, strategy.take_profit_atr_multiple
            ),
            commission_rate=costs.commission_rate,
        ),
        costs=costs,
        gap_threshold_atr=arguments.gap_threshold_atr,
    )
    return BacktestRunRequest(
        symbols=resolve_symbols(arguments, settings),
        start=arguments.start,
        end=arguments.end or default_end_date(),
        parameters=parameters,
        source=arguments.source,
        adjustment_version=arguments.adjustment_version,
        include_dividends=not arguments.no_dividends,
        reference_symbols=(
            settings.universe(settings.signals.reference_universe)
            if needs_reference_series(arguments)
            else ()
        ),
    )


def resolve_symbols(
    arguments: argparse.Namespace,
    settings: ApplicationSettings,
) -> tuple[str, ...]:
    """`--symbols` explícitos, o los del universo seleccionado."""
    if arguments.symbols:
        return tuple(symbol.upper() for symbol in arguments.symbols)

    return settings.universe(selected_universe(arguments, settings) or "")


def selected_universe(
    arguments: argparse.Namespace,
    settings: ApplicationSettings,
) -> str | None:
    """`--universe`, o el universo por defecto si no hay `--symbols`."""
    if arguments.symbols:
        return None

    return (arguments.universe or settings.default_universe).lower()


def build_grid(
    arguments: argparse.Namespace,
    request: BacktestRunRequest,
) -> ParameterGrid:
    """Rejilla de la CLI; sin `--positions-grid`/`--risk-grid` usa los valores base."""
    risk = request.parameters.risk
    return ParameterGrid(
        stop_loss_atr_multiples=tuple(arguments.sl_multiples),
        take_profit_atr_multiples=tuple(arguments.tp_multiples),
        max_position_exposures=tuple(arguments.exposures),
        max_positions=tuple(arguments.positions_grid or (risk.max_positions,)),
        risk_per_trade=tuple(arguments.risk_grid or (risk.risk_per_trade,)),
        use_sector_filter=sector_filter_grid(arguments),
    )


def report_label(
    prefix: str,
    arguments: argparse.Namespace,
    request: BacktestRunRequest,
    settings: ApplicationSettings,
) -> str:
    """`<prefijo>[_<etiqueta>]_<universo o símbolos>_<inicio>_<fin>`."""
    for tag in (arguments.report_tag, signal_filter_tag(arguments)):
        if tag:
            prefix = f"{prefix}_{tag}"

    universe = selected_universe(arguments, settings)

    if universe:
        return (
            f"{prefix}_{universe}_{request.start.isoformat()}_{request.end.isoformat()}"
        )

    return prefix + default_report_name(
        request.symbols, request.start, request.end
    ).removeprefix("backtest")


def run_backtest_command(
    arguments: argparse.Namespace,
    session_factory: Callable[[], Session],
    settings: ApplicationSettings,
    calendar: TradingCalendar | None,
) -> tuple[BacktestRunOutcome, Path, Path]:
    request = build_request(arguments, settings)

    with session_factory() as session:
        outcome = BacktestRunService(session, calendar=calendar).run(
            request,
            persist=not arguments.no_persist,
            signal_generator=build_signal_generator(arguments, settings),
        )

    name = arguments.name or report_label("backtest", arguments, request, settings)
    json_path, markdown_path = write_report(outcome.report, arguments.output_dir, name)
    return outcome, json_path, markdown_path


def run_sweep_command(
    arguments: argparse.Namespace,
    session_factory: Callable[[], Session],
    settings: ApplicationSettings,
    calendar: TradingCalendar | None,
) -> tuple[SensitivityReport, Path, Path]:
    request = build_request(arguments, settings)
    grid = build_grid(arguments, request)

    with session_factory() as session:
        report = SensitivityAnalysisService(
            BacktestRunService(session, calendar=calendar)
        ).run(request, grid, persist=not arguments.no_persist)

    name = arguments.name or (
        report_label("sensitivity", arguments, request, settings)
        + f"_{request.parameters.cost_profile}"
    )
    json_path, markdown_path = write_sensitivity_report(
        report,
        arguments.output_dir,
        name,
    )
    return report, json_path, markdown_path


def run_walk_forward_command(
    arguments: argparse.Namespace,
    session_factory: Callable[[], Session],
    settings: ApplicationSettings,
    calendar: TradingCalendar | None,
) -> tuple[WalkForwardReport, Path, Path]:
    """
    Carga los datos una vez de MySQL y ejecuta el walk-forward en memoria.

    Con el filtro sectorial en la rejilla, las series de referencia se leen
    en la misma carga y el analizador las reutiliza en todas las ventanas.
    """
    request = build_request(arguments, settings)
    config = WalkForwardConfig(
        in_sample_months=arguments.is_months,
        out_of_sample_months=arguments.oos_months,
        step_months=arguments.wf_step_months,
        selection_metric=SelectionMetric(arguments.wf_metric),
        min_trades=arguments.min_trades,
        grid=build_grid(arguments, request),
    )

    with session_factory() as session:
        data = BacktestRunService(session, calendar=calendar).prepare(request)

    report = WalkForwardAnalyzer(
        data.bars_by_symbol,
        dividends_by_symbol=data.dividends_by_symbol,
        trading_sessions=data.trading_sessions,
        trading_calendar=calendar.name if calendar is not None else None,
        reference_bars=data.reference_bars,
        sector_filter=(
            build_sector_filter(settings)
            if config.grid.includes_sector_filter
            else None
        ),
    ).run(
        request.parameters,
        config,
        start=request.start,
        end=request.end,
        universe=selected_universe(arguments, settings),
    )

    name = arguments.name or (
        report_label("walk_forward", arguments, request, settings)
        + f"_{request.parameters.cost_profile}"
        + f"_is{config.in_sample_months}-oos{config.out_of_sample_months}"
        + f"_{config.selection_metric}"
    )
    json_path, markdown_path = write_walk_forward_report(
        report,
        arguments.output_dir,
        name,
    )
    return report, json_path, markdown_path


def format_summary(
    outcome: BacktestRunOutcome,
    json_path: Path,
    markdown_path: Path,
) -> str:
    report = outcome.report
    metrics = report.metrics
    trades = metrics.trades
    buy_and_hold = metrics.buy_and_hold
    risk = report.parameters.risk

    lines = [
        f"Ejecución: {outcome.run_id or 'no guardada'}",
        f"Símbolos: {', '.join(report.symbols)}",
        f"Periodo: {report.start} → {report.end}  "
        f"Calendario: {report.trading_calendar or 'ninguno'}",
        f"Costes: {report.parameters.cost_profile or 'n/d'}  "
        f"SL: {risk.stop_loss_atr_multiple} × ATR  "
        f"Exposición máx.: {_pct(risk.max_position_exposure)}",
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

    if report.signal_filters is not None:
        summary = report.signal_filters
        blocked = ", ".join(
            f"{name} {count}" for name, count in summary.filtered_by.items()
        )
        lines.append(
            f"Filtros v2: {summary.strategy_id}  "
            f"Señales bloqueadas: {summary.filtered_signals}"
            + (f" ({blocked})" if blocked else "")
        )

    lines.extend([f"Informe JSON: {json_path}", f"Informe Markdown: {markdown_path}"])
    return "\n".join(lines)


def format_sweep_summary(
    report: SensitivityReport,
    json_path: Path,
    markdown_path: Path,
) -> str:
    parameters = report.base_parameters
    lines = [
        f"Barrido: {report.sweep_id or 'no guardado'}",
        f"Símbolos: {', '.join(report.symbols)}  Periodo: {report.start} → "
        f"{report.end}",
        f"Costes: {parameters.cost_profile}  "
        f"Buy & Hold: {_pct(report.buy_and_hold_return)}",
        "",
        *sensitivity_table(report.rows),
        "",
        f"Informe JSON: {json_path}",
        f"Informe Markdown: {markdown_path}",
    ]
    return "\n".join(lines)


def format_walk_forward_summary(
    report: WalkForwardReport,
    json_path: Path,
    markdown_path: Path,
) -> str:
    config = report.config
    oos = report.out_of_sample
    lines = [
        f"Walk-forward: IS {config.in_sample_months} m / OOS "
        f"{config.out_of_sample_months} m, selección por {config.selection_metric}",
        f"Símbolos: {', '.join(report.symbols)}  "
        f"Costes: {report.base_parameters.cost_profile}",
        "",
        f"{'#':>2} {'OOS':<25} {'SL':>4} {'TP':>4} {'exp.':>7} {'pos.':>4} "
        f"{'riesgo':>7} {'anual. IS':>10} "
        f"{'anual. OOS':>10} {'PF OOS':>6} {'trades':>6} {'WFE':>9}",
    ]

    for result in report.windows:
        window = result.window
        lines.append(
            f"{window.index:>2} {window.out_of_sample_start} → "
            f"{window.out_of_sample_end} "
            f"{result.selected.stop_loss_atr_multiple!s:>4} "
            f"{result.selected.take_profit_atr_multiple!s:>4} "
            f"{_pct(result.selected.max_position_exposure):>7} "
            f"{result.selected.max_positions:>4} "
            f"{_pct(result.selected.risk_per_trade):>7} "
            f"{_pct(result.selected.annualized_return):>10} "
            f"{_pct(result.out_of_sample.annualized_return):>10} "
            f"{_ratio(result.out_of_sample.profit_factor):>6} "
            f"{result.out_of_sample.total_trades:>6} "
            f"{_pct(result.walk_forward_efficiency):>9}"
        )

    robust = {True: "sí", False: "no", None: "n/d"}[report.is_robust]
    benchmark = report.buy_and_hold
    benchmark_return = benchmark.cumulative_return if benchmark else None
    excess_return = benchmark.strategy_excess_return if benchmark else None
    lines.extend(
        [
            "",
            f"OOS encadenado: retorno {_pct(oos.cumulative_return)}  "
            f"anualizado {_pct(oos.annualized_return)}  "
            f"MDD {_pct(oos.max_drawdown_pct)}  PF {_ratio(oos.profit_factor)}  "
            f"Sharpe {_ratio(oos.sharpe_ratio)}  trades {oos.total_trades}",
            f"IS anualizado medio: {_pct(report.mean_in_sample_annualized_return)}  "
            f"WFE: {_pct(report.walk_forward_efficiency)}  "
            f"Robusta (WFE > 50 %): {robust}",
            f"Uso de capital OOS: tiempo en mercado {_pct(oos.time_in_market)}  "
            f"exposición media {_pct(oos.average_exposure)}",
            f"Buy & Hold OOS: {_pct(benchmark_return)}  "
            f"Exceso de la estrategia: {_pct(excess_return)}",
            *_scaling_lines(report),
            *_sector_filter_lines(report),
            f"Informe JSON: {json_path}",
            f"Informe Markdown: {markdown_path}",
        ]
    )
    return "\n".join(lines)


def _scaling_lines(report: WalkForwardReport) -> list[str]:
    if not report.scaling_study:
        return []

    lines = [
        "",
        "Escalado de cartera (walk-forward por nivel):",
        f"{'pos.':>4} {'riesgo':>7} {'retorno':>9} {'MDD':>8} {'Sharpe':>6} "
        f"{'t. mercado':>10} {'exp. media':>10} {'WFE':>9}",
    ]
    lines.extend(
        f"{level.max_positions:>4} {_pct(level.risk_per_trade):>7} "
        f"{_pct(level.out_of_sample.cumulative_return):>9} "
        f"{_pct(level.out_of_sample.max_drawdown_pct):>8} "
        f"{_ratio(level.out_of_sample.sharpe_ratio):>6} "
        f"{_pct(level.out_of_sample.time_in_market):>10} "
        f"{_pct(level.out_of_sample.average_exposure):>10} "
        f"{_pct(level.walk_forward_efficiency):>9}"
        for level in report.scaling_study
    )
    lines.append("")
    return lines


def format_runs(runs: Sequence[BacktestRunSummary]) -> str:
    if not runs:
        return "No hay ejecuciones guardadas."

    lines = [
        f"{'run_id':<36} {'creado (UTC)':<19} {'periodo':<23} {'costes':<11} "
        f"{'SL':>4} {'exp.':>5} {'gap':>4} {'retorno':>9} {'MDD':>8} "
        f"{'trades':>6} {'PF':>5} {'B&H':>9} sweep"
    ]

    for run in runs:
        parameters = run.parameters
        risk = parameters.get("risk", {})
        lines.append(
            f"{run.run_id:<36} {run.created_at:%Y-%m-%d %H:%M:%S} "
            f"{run.start_date} → {run.end_date} "
            f"{parameters.get('cost_profile') or '-':<11} "
            f"{risk.get('stop_loss_atr_multiple', '-')!s:>4} "
            f"{risk.get('max_position_exposure', '-')!s:>5} "
            f"{parameters.get('gap_threshold_atr', '-')!s:>4} "
            f"{_pct(run.cumulative_return):>9} {_pct(run.max_drawdown_pct):>8} "
            f"{run.total_trades:>6} {_ratio(run.profit_factor):>5} "
            f"{_pct(run.buy_and_hold_return):>9} {(run.sweep_id or '-')[:8]}"
        )

    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)

    from algo_trading.persistence.database import SessionFactory

    if arguments.list_runs is not None:
        with SessionFactory() as session:
            runs = BacktestRunRepository(session).list_summaries(
                arguments.list_runs,
                sweep_id=arguments.sweep_id,
            )
        print(format_runs(runs))
        return 0

    settings = ApplicationSettings.from_yaml()
    calendar = build_calendar(arguments.calendar)

    if arguments.walk_forward:
        wf_report, json_path, markdown_path = run_walk_forward_command(
            arguments,
            session_factory=SessionFactory,
            settings=settings,
            calendar=calendar,
        )
        print(format_walk_forward_summary(wf_report, json_path, markdown_path))
        return 0

    if arguments.sweep:
        report, json_path, markdown_path = run_sweep_command(
            arguments,
            session_factory=SessionFactory,
            settings=settings,
            calendar=calendar,
        )
        print(format_sweep_summary(report, json_path, markdown_path))
        return 0

    outcome, json_path, markdown_path = run_backtest_command(
        arguments,
        session_factory=SessionFactory,
        settings=settings,
        calendar=calendar,
    )
    print(format_summary(outcome, json_path, markdown_path))
    return 0


def _sector_filter_lines(report: WalkForwardReport) -> list[str]:
    if not report.sector_filter_study:
        return []

    lines = [
        f"Filtro sectorial elegido en {report.sector_filter_selections} de "
        f"{len(report.windows)} ventanas IS; señales bloqueadas "
        f"{report.sector_filter_blocked_signals}"
    ]
    lines.extend(
        f"  {'con' if study.use_sector_filter else 'sin'} filtro: retorno OOS "
        f"{_pct(study.out_of_sample.cumulative_return)}  "
        f"MDD {_pct(study.out_of_sample.max_drawdown_pct)}  "
        f"PF {_ratio(study.out_of_sample.profit_factor)}  "
        f"WFE {_pct(study.walk_forward_efficiency)}"
        for study in report.sector_filter_study
    )
    return lines


def _or_default(value: Decimal | None, default: Decimal) -> Decimal:
    return value if value is not None else default


def _pct(value: Decimal | None) -> str:
    return "n/d" if value is None else f"{value * 100:.2f} %"


def _ratio(value: Decimal | None) -> str:
    return "n/d" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    raise SystemExit(main())
