"""
Comparación de un backtest con resolución intrabarra frente a la regla de la v1.

Ambos backtests usan los mismos datos diarios y parámetros; sólo cambia cómo
se resuelven las velas que tocan SL y TP (la v1 aplica siempre el SL). Un
cambio de SL a TP altera el cash y, por tanto, el tamaño y la disponibilidad
de plazas de las operaciones posteriores: `changed_trades` cuenta todas las
operaciones que no coinciden, no sólo las resueltas con datos intradía.
"""

from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.analytics.report import BacktestReport
from algo_trading.data.providers.alpaca import AlpacaRequestStats

REPORT_VERSION = "intrabar-comparison-v1"
NOT_AVAILABLE = "n/d"


class ComparedMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    final_equity: Decimal
    cumulative_return: Decimal
    max_drawdown_pct: Decimal
    win_rate: Decimal | None
    profit_factor: Decimal | None
    expectancy_r: Decimal | None
    total_trades: int
    exits_by_reason: dict[str, int]

    @classmethod
    def from_report(cls, report: BacktestReport) -> "ComparedMetrics":
        metrics = report.metrics
        return cls(
            final_equity=metrics.final_equity,
            cumulative_return=metrics.cumulative_return,
            max_drawdown_pct=metrics.max_drawdown.max_drawdown_pct,
            win_rate=metrics.trades.win_rate,
            profit_factor=metrics.trades.profit_factor,
            expectancy_r=metrics.trades.expectancy_r,
            total_trades=metrics.trades.total_trades,
            exits_by_reason=dict(metrics.trades.exits_by_reason),
        )


class IntrabarComparisonReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report_version: str = REPORT_VERSION
    symbols: tuple[str, ...]
    start: str | None
    end: str | None
    v1_report_checksum: str
    intrabar_report_checksum: str
    intrabar_run_id: str | None
    conflicts: int = Field(ge=0)
    take_profit_first: int = Field(ge=0)
    stop_loss_first: int = Field(ge=0)
    fallbacks: int = Field(ge=0)
    by_resolution: dict[str, int]
    sessions_requested: int = Field(ge=0)
    changed_trades: int = Field(ge=0)
    v1: ComparedMetrics
    intrabar: ComparedMetrics
    api: AlpacaRequestStats | None
    elapsed_seconds: float = Field(ge=0)


def build_intrabar_comparison(
    v1_report: BacktestReport,
    v1_checksum: str,
    intrabar_report: BacktestReport,
    intrabar_checksum: str,
    intrabar_run_id: str | None,
    sessions_requested: int,
    api: AlpacaRequestStats | None,
    elapsed_seconds: float,
) -> IntrabarComparisonReport:
    summary = intrabar_report.intrabar

    if summary is None:
        raise ValueError("El informe intrabarra no tiene resolución intrabarra")

    v1_trades = set(v1_report.trades)
    return IntrabarComparisonReport(
        symbols=intrabar_report.symbols,
        start=intrabar_report.start,
        end=intrabar_report.end,
        v1_report_checksum=v1_checksum,
        intrabar_report_checksum=intrabar_checksum,
        intrabar_run_id=intrabar_run_id,
        conflicts=summary.conflicts,
        take_profit_first=summary.take_profit_first,
        stop_loss_first=summary.stop_loss_first,
        fallbacks=summary.fallbacks,
        by_resolution=summary.by_resolution,
        sessions_requested=sessions_requested,
        changed_trades=sum(
            1 for trade in intrabar_report.trades if trade not in v1_trades
        ),
        v1=ComparedMetrics.from_report(v1_report),
        intrabar=ComparedMetrics.from_report(intrabar_report),
        api=api,
        elapsed_seconds=elapsed_seconds,
    )


def intrabar_comparison_to_markdown(report: IntrabarComparisonReport) -> str:
    v1 = report.v1
    intrabar = report.intrabar
    api = report.api

    def row(label: str, before: str, after: str, delta: str) -> str:
        return f"| {label} | {before} | {after} | {delta} |"

    lines = [
        "# Resolución intrabarra SL / TP frente a la v1",
        "",
        f"- Símbolos: {', '.join(report.symbols)}",
        f"- Periodo: {report.start or NOT_AVAILABLE} → {report.end or NOT_AVAILABLE}",
        f"- Ejecución intrabarra: `{report.intrabar_run_id or 'no guardada'}`",
        f"- Checksum v1 (sin resolución): `{report.v1_report_checksum}`",
        "",
        "## Velas con SL y TP tocados",
        "",
        "| Métrica | Valor |",
        "| --- | ---: |",
        f"| Sesiones con conflicto SL / TP | {report.conflicts} |",
        f"| Resueltas TP primero (la v1 daba SL) | {report.take_profit_first} |",
        f"| Resueltas SL primero (confirman la v1) | {report.stop_loss_first} |",
        f"| Fallback a la regla v1 | {report.fallbacks} |",
        f"| Sesiones intradía descargadas | {report.sessions_requested} |",
        "",
        "Por resolución: "
        + (
            ", ".join(f"{name} {count}" for name, count in report.by_resolution.items())
            or NOT_AVAILABLE
        ),
        "",
        "## Impacto en el backtest",
        "",
        "| Métrica | v1 (SL primero) | Intrabarra | Diferencia |",
        "| --- | ---: | ---: | ---: |",
        row(
            "Equity final",
            _money(v1.final_equity),
            _money(intrabar.final_equity),
            _money(intrabar.final_equity - v1.final_equity),
        ),
        row(
            "Retorno acumulado",
            _pct(v1.cumulative_return),
            _pct(intrabar.cumulative_return),
            _pct_points(intrabar.cumulative_return - v1.cumulative_return),
        ),
        row(
            "Maximum Drawdown",
            _pct(v1.max_drawdown_pct),
            _pct(intrabar.max_drawdown_pct),
            _pct_points(intrabar.max_drawdown_pct - v1.max_drawdown_pct),
        ),
        row(
            "Win Rate",
            _pct(v1.win_rate),
            _pct(intrabar.win_rate),
            _difference(intrabar.win_rate, v1.win_rate, _pct_points),
        ),
        row(
            "Profit Factor",
            _ratio(v1.profit_factor),
            _ratio(intrabar.profit_factor),
            _difference(intrabar.profit_factor, v1.profit_factor, _signed_ratio),
        ),
        row(
            "Expectancy en R",
            _ratio(v1.expectancy_r),
            _ratio(intrabar.expectancy_r),
            _difference(intrabar.expectancy_r, v1.expectancy_r, _signed_ratio),
        ),
        row(
            "Trades",
            str(v1.total_trades),
            str(intrabar.total_trades),
            f"{intrabar.total_trades - v1.total_trades:+d}",
        ),
        "",
        "Salidas: v1 "
        + _exits(v1.exits_by_reason)
        + "; intrabarra "
        + _exits(intrabar.exits_by_reason)
        + ".",
        f"Trades que no coinciden con la v1: {report.changed_trades} (incluye "
        "los efectos de cash y plazas sobre operaciones posteriores).",
        "",
        "## API de Alpaca",
        "",
    ]

    if api is None:
        lines.append("Sin proveedor Alpaca.")
    else:
        lines.extend(
            [
                "| Métrica | Valor |",
                "| --- | ---: |",
                f"| Peticiones (incluidos reintentos) | {api.requests} |",
                f"| Respuestas correctas | {api.successful} |",
                f"| 429 (cuota) / 5xx / red / 4xx | {api.rate_limited} / "
                f"{api.server_errors} / {api.network_errors} / {api.client_errors} |",
                f"| Latencia media por petición | {_ms(api.average_latency_ms)} |",
                f"| Latencia máxima | {_ms(api.max_latency_ms)} |",
                f"| Espera por cuota y backoff | {api.slept_seconds:.1f} s |",
                f"| Cuota por minuto (X-RateLimit-Limit) | "
                f"{api.rate_limit_limit or NOT_AVAILABLE} |",
                f"| Mínimo restante visto (X-RateLimit-Remaining) | "
                f"{_optional(api.min_rate_limit_remaining)} |",
                f"| Tiempo total (dos backtests y descargas) | "
                f"{report.elapsed_seconds:.1f} s |",
            ]
        )

    lines.extend(
        [
            "",
            "## Notas",
            "",
            "- Sólo se descargan las sesiones en las que la vela diaria toca SL y "
            "TP y la apertura no decide ya el orden.",
            "- Feed IEX: volumen parcial. Si el SL diario no aparece en los "
            "minutos, el orden no se considera conocido y se aplica la v1.",
            "- Precios de 1 minuto ajustados por splits (escala `splits-v1`).",
            "",
        ]
    )
    return "\n".join(lines)


def write_intrabar_comparison(
    report: IntrabarComparisonReport,
    directory: Path,
    stem: str,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(intrabar_comparison_to_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _difference(
    after: Decimal | None,
    before: Decimal | None,
    formatter: Callable[[Decimal], str],
) -> str:
    if after is None or before is None:
        return NOT_AVAILABLE

    return formatter(after - before)


def _exits(exits: dict[str, int]) -> str:
    return ", ".join(f"{reason} {count}" for reason, count in exits.items()) or (
        NOT_AVAILABLE
    )


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN):,}"


def _pct(value: Decimal | None) -> str:
    if value is None:
        return NOT_AVAILABLE

    return f"{(value * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)} %"


def _pct_points(value: Decimal) -> str:
    points = (value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"{points:+} pp"


def _ratio(value: Decimal | None) -> str:
    if value is None:
        return NOT_AVAILABLE

    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN)}"


def _optional(value: int | None) -> str:
    return NOT_AVAILABLE if value is None else str(value)


def _signed_ratio(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN):+}"


def _ms(value: float | None) -> str:
    return NOT_AVAILABLE if value is None else f"{value:.0f} ms"
