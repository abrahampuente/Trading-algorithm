from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.analytics.metrics import (
    PerformanceMetrics,
    calculate_performance_metrics,
)
from algo_trading.backtesting.engine import (
    BacktestParameters,
    BacktestResult,
    DividendPayment,
    EquityPoint,
    ExecutionBar,
    GapFilteredEntry,
)
from algo_trading.domain.corporate_actions.dto import CorporateDividend
from algo_trading.engine.intrabar import IntrabarResolutionRecord
from algo_trading.execution.exit_rules import ExitReason
from algo_trading.execution.trade import Trade
from algo_trading.portfolio.allocator import RejectedCandidate
from algo_trading.signals import CompositeSignalGenerator, SignalGenerator

REPORT_VERSION = "backtest-report-v2"
NOT_AVAILABLE = "n/d"


class SignalFilterSummary(BaseModel):
    """
    Generador multi-factor usado en el backtest y señales que bloqueó.

    `filtered_by` cuenta, por filtro, las señales que no superó;
    `filtered_reasons` cuenta los motivos por falta de datos.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    filters: tuple[dict[str, Any], ...]
    filtered_signals: int = Field(ge=0)
    filtered_by: dict[str, int]
    filtered_reasons: dict[str, int]


class IntrabarSummary(BaseModel):
    """
    Velas con SL y TP tocados y cómo se resolvieron con datos intradía.

    `take_profit_first` son las que la v1 habría cerrado por SL y la
    trayectoria intradía cerró por TP; `fallbacks`, las resueltas con la
    política de la v1 por falta de datos o de orden.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    conflicts: int = Field(ge=0)
    take_profit_first: int = Field(ge=0)
    stop_loss_first: int = Field(ge=0)
    fallbacks: int = Field(ge=0)
    by_resolution: dict[str, int]
    records: tuple[IntrabarResolutionRecord, ...]


class BacktestReport(BaseModel):
    """
    Resumen exportable y reproducible de un backtest.

    No incluye la fecha de generación: el mismo backtest produce siempre el
    mismo informe. Los Decimal se serializan en JSON como texto para no
    perder precisión.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    report_version: str = REPORT_VERSION
    symbols: tuple[str, ...]
    start: str | None
    end: str | None
    trading_calendar: str | None = None
    parameters: BacktestParameters
    metrics: PerformanceMetrics
    trades: tuple[Trade, ...]
    gap_filtered: tuple[GapFilteredEntry, ...]
    rejected: tuple[RejectedCandidate, ...]
    dividends: tuple[DividendPayment, ...] = ()
    equity_curve: tuple[EquityPoint, ...]
    # Ausente del JSON sin generador multi-factor: el informe (y su checksum)
    # de la v1 no cambia.
    signal_filters: SignalFilterSummary | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    # Igual que `signal_filters`: ausente sin resolución intrabarra.
    intrabar: IntrabarSummary | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def build_report(
    result: BacktestResult,
    bars_by_symbol: Mapping[str, Sequence[ExecutionBar]],
    dividends_by_symbol: Mapping[str, Sequence[CorporateDividend]] | None = None,
    trading_calendar: str | None = None,
    signal_generator: SignalGenerator | None = None,
) -> BacktestReport:
    curve = result.equity_curve

    return BacktestReport(
        symbols=tuple(sorted(bars_by_symbol)),
        start=curve[0].timestamp.isoformat() if curve else None,
        end=curve[-1].timestamp.isoformat() if curve else None,
        trading_calendar=trading_calendar,
        parameters=result.parameters,
        metrics=calculate_performance_metrics(
            result,
            bars_by_symbol,
            dividends_by_symbol,
        ),
        trades=result.trades,
        gap_filtered=result.gap_filtered,
        rejected=result.rejected,
        dividends=result.dividends,
        equity_curve=curve,
        signal_filters=summarize_signal_filters(result, signal_generator),
        intrabar=summarize_intrabar(result),
    )


def summarize_intrabar(result: BacktestResult) -> IntrabarSummary | None:
    if not result.uses_intrabar_resolution:
        return None

    records = result.intrabar_resolutions
    fallbacks = [record for record in records if record.resolution.is_fallback]
    resolved = [record for record in records if not record.resolution.is_fallback]
    return IntrabarSummary(
        conflicts=len(records),
        take_profit_first=sum(
            record.exit_reason is ExitReason.TAKE_PROFIT for record in resolved
        ),
        stop_loss_first=sum(
            record.exit_reason is ExitReason.STOP_LOSS for record in resolved
        ),
        fallbacks=len(fallbacks),
        by_resolution=dict(
            sorted(Counter(str(record.resolution) for record in records).items())
        ),
        records=records,
    )


def summarize_signal_filters(
    result: BacktestResult,
    signal_generator: SignalGenerator | None,
) -> SignalFilterSummary | None:
    if result.signal_strategy_id is None:
        return None

    filters = (
        signal_generator.filters
        if isinstance(signal_generator, CompositeSignalGenerator)
        else ()
    )
    failed = [
        filter_result
        for filtered in result.filtered_signals
        for filter_result in filtered.results
        if not filter_result.passed
    ]
    return SignalFilterSummary(
        strategy_id=result.signal_strategy_id,
        filters=tuple(
            {
                "name": signal_filter.name,
                **(
                    signal_filter.model_dump(mode="json")
                    if isinstance(signal_filter, BaseModel)
                    else {}
                ),
            }
            for signal_filter in filters
        ),
        filtered_signals=len(result.filtered_signals),
        filtered_by=dict(sorted(Counter(item.name for item in failed).items())),
        filtered_reasons=dict(
            sorted(Counter(item.reason for item in failed if item.reason).items())
        ),
    )


def report_to_json(report: BacktestReport) -> str:
    return report.model_dump_json(indent=2)


def _intrabar_lines(summary: IntrabarSummary | None) -> list[str]:
    if summary is None:
        return []

    return [
        "## Resolución intrabarra SL / TP (v2)",
        "",
        f"- Velas con SL y TP tocados: {summary.conflicts}",
        f"- Resueltas por la trayectoria intradía: TP primero "
        f"{summary.take_profit_first}, SL primero {summary.stop_loss_first}",
        f"- Fallback a la política v1 (SL): {summary.fallbacks}",
        "- Por resolución: "
        + (
            ", ".join(
                f"{name} {count}" for name, count in summary.by_resolution.items()
            )
            or NOT_AVAILABLE
        ),
        "",
    ]


def _signal_filter_lines(summary: SignalFilterSummary | None) -> list[str]:
    if summary is None:
        return []

    filters = ", ".join(str(item["name"]) for item in summary.filters)
    return [
        "## Filtros de señal (v2)",
        "",
        f"- Estrategia: `{summary.strategy_id}`",
        f"- Filtros: {filters or NOT_AVAILABLE}",
        f"- Señales bloqueadas: {summary.filtered_signals}",
        "- Bloqueos por filtro: "
        + (
            ", ".join(f"{name} {count}" for name, count in summary.filtered_by.items())
            or NOT_AVAILABLE
        ),
        "- Motivos por falta de datos: "
        + (
            ", ".join(
                f"{reason} {count}"
                for reason, count in summary.filtered_reasons.items()
            )
            or NOT_AVAILABLE
        ),
        "",
    ]


def report_to_markdown(report: BacktestReport) -> str:
    metrics = report.metrics
    trades = metrics.trades
    parameters = report.parameters
    drawdown = metrics.max_drawdown
    buy_and_hold = metrics.buy_and_hold

    lines = [
        "# Informe de backtest",
        "",
        f"- Versión del informe: `{report.report_version}`",
        f"- Símbolos: {', '.join(report.symbols) or NOT_AVAILABLE}",
        f"- Periodo: {report.start or NOT_AVAILABLE} → {report.end or NOT_AVAILABLE}",
        f"- Calendario de mercado: {report.trading_calendar or 'sin calendario'}",
        "",
        "## Parámetros",
        "",
        "| Parámetro | Valor |",
        "| --- | --- |",
        f"| Capital inicial | {_money(parameters.initial_cash)} |",
        f"| Bollinger | {parameters.signal.indicators.bollinger_window} sesiones, "
        f"k = {parameters.signal.indicators.bollinger_std_multiplier} |",
        f"| ATR | {parameters.signal.indicators.atr_window} sesiones (Wilder) |",
        f"| Momentum | t-{parameters.signal.momentum_short_lookback} / "
        f"t-{parameters.signal.momentum_long_lookback} |",
        f"| Riesgo por operación | {_pct(parameters.risk.risk_per_trade)} |",
        f"| Exposición máxima por posición | "
        f"{_pct(parameters.risk.max_position_exposure)} |",
        f"| Máximo de posiciones | {parameters.risk.max_positions} |",
        f"| Stop Loss | {parameters.risk.stop_loss_atr_multiple} × ATR |",
        f"| Take Profit | {parameters.risk.take_profit_atr_multiple} × ATR |",
        f"| Perfil de costes | {parameters.cost_profile or NOT_AVAILABLE} |",
        f"| Comisión | {_pct(parameters.costs.commission_rate)} |",
        f"| Slippage | {_pct(parameters.costs.slippage_rate)} |",
        f"| Umbral anti-gap | {parameters.gap_threshold_atr} × ATR |",
        f"| Salida temporal | {parameters.max_holding_sessions} sesiones |",
        "",
        "## Resultados",
        "",
        "| Métrica | Estrategia | Buy & Hold |",
        "| --- | --- | --- |",
        f"| Equity final | {_money(metrics.final_equity)} | "
        f"{_money(buy_and_hold.final_equity if buy_and_hold else None)} |",
        f"| Retorno acumulado | {_pct(metrics.cumulative_return)} | "
        f"{_pct(buy_and_hold.cumulative_return if buy_and_hold else None)} |",
        f"| Dividendos cobrados | {_money(metrics.dividend_income)} | "
        f"{_money(buy_and_hold.dividend_income if buy_and_hold else None)} |",
        f"| Maximum Drawdown | {_pct(drawdown.max_drawdown_pct)} | "
        f"{_pct(buy_and_hold.max_drawdown.max_drawdown_pct if buy_and_hold else None)}"
        " |",
        "",
        f"- Beneficio neto: {_money(metrics.net_profit)}",
        "- Exceso de retorno frente a Buy & Hold: "
        f"{_pct(buy_and_hold.strategy_excess_return if buy_and_hold else None)}",
        f"- Drawdown máximo en importe: {_money(drawdown.max_drawdown_amount)} "
        f"(pico {drawdown.peak_timestamp or 'capital inicial'}, "
        f"valle {drawdown.trough_timestamp or NOT_AVAILABLE})",
        f"- Tiempo en mercado: {_pct(metrics.exposure.time_in_market)}",
        f"- Exposición media: {_pct(metrics.exposure.average_exposure)}",
        "",
        "## Operaciones",
        "",
        "| Métrica | Valor |",
        "| --- | --- |",
        f"| Trades | {trades.total_trades} |",
        f"| Ganadores / perdedores | {trades.winning_trades} / "
        f"{trades.losing_trades} |",
        f"| Win Rate | {_pct(trades.win_rate)} |",
        f"| Profit Factor | {_ratio(trades.profit_factor)} |",
        f"| Expectancy por trade | {_money(trades.expectancy)} |",
        f"| Expectancy en R | {_ratio(trades.expectancy_r)} |",
        f"| Ganancia media | {_money(trades.average_win)} |",
        f"| Pérdida media | {_money(trades.average_loss)} |",
        f"| Duración media (sesiones) | {_ratio(trades.average_sessions_held)} |",
        f"| Duración media (días naturales) | {_ratio(trades.average_holding_days)} |",
        f"| Costes totales | {_money(trades.total_costs)} |",
        f"| Entradas GAP_FILTERED | {metrics.gap_filtered_entries} |",
        f"| Entradas rechazadas por portfolio | {metrics.rejected_entries} |",
        "",
        "Motivos de salida: "
        + (
            ", ".join(
                f"{reason} {count}" for reason, count in trades.exits_by_reason.items()
            )
            or NOT_AVAILABLE
        ),
        "",
        *_signal_filter_lines(report.signal_filters),
        *_intrabar_lines(report.intrabar),
        "## Registro de trades",
        "",
    ]

    if report.trades:
        lines.extend(
            [
                "| Símbolo | Entrada | Salida | Cantidad | Entrada ejec. "
                "| Salida ejec. | PnL bruto | Costes | PnL neto | Motivo |",
                "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        lines.extend(
            f"| {trade.symbol} | {trade.entry_timestamp.date()} "
            f"| {trade.exit_timestamp.date()} | {trade.quantity} "
            f"| {_money(trade.entry_executed_price)} "
            f"| {_money(trade.exit_executed_price)} | {_money(trade.gross_pnl)} "
            f"| {_money(trade.total_costs)} | {_money(trade.net_pnl)} "
            f"| {trade.exit_reason} |"
            for trade in report.trades
        )
    else:
        lines.append("Sin operaciones.")

    lines.append("")
    return "\n".join(lines)


def write_report(
    report: BacktestReport,
    directory: Path,
    stem: str = "backtest_report",
) -> tuple[Path, Path]:
    """Escribe el informe en `<stem>.json` y `<stem>.md` dentro de `directory`."""
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_text(report_to_json(report), encoding="utf-8")
    markdown_path.write_text(report_to_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _money(value: Decimal | None) -> str:
    if value is None:
        return NOT_AVAILABLE

    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN):,}"


def _pct(value: Decimal | None) -> str:
    if value is None:
        return NOT_AVAILABLE

    percentage = (value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"{percentage} %"


def _ratio(value: Decimal | None) -> str:
    if value is None:
        return NOT_AVAILABLE

    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
