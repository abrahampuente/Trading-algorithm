import uuid
from collections.abc import Callable, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import product
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algo_trading.backtesting.engine import BacktestParameters
from algo_trading.backtesting.run_service import (
    BacktestRunRequest,
    BacktestRunService,
)

DEFAULT_STOP_LOSS_ATR_MULTIPLES = (Decimal("1.0"), Decimal("1.5"), Decimal("2.0"))
DEFAULT_MAX_POSITION_EXPOSURES = (Decimal("0.10"), Decimal("0.15"), Decimal("0.20"))
NOT_AVAILABLE = "n/d"


class ParameterGrid(BaseModel):
    """Rejilla de parámetros a evaluar: producto cartesiano de ambos ejes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_loss_atr_multiples: tuple[Decimal, ...] = Field(
        default=DEFAULT_STOP_LOSS_ATR_MULTIPLES,
        min_length=1,
    )
    max_position_exposures: tuple[Decimal, ...] = Field(
        default=DEFAULT_MAX_POSITION_EXPOSURES,
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_grid(self) -> Self:
        if any(multiple <= 0 for multiple in self.stop_loss_atr_multiples):
            raise ValueError("Los multiplicadores de stop loss deben ser mayores que 0")

        if any(not 0 < exposure <= 1 for exposure in self.max_position_exposures):
            raise ValueError("Las exposiciones deben estar en (0, 1]")

        for name, values in (
            ("stop_loss_atr_multiples", self.stop_loss_atr_multiples),
            ("max_position_exposures", self.max_position_exposures),
        ):
            if len(set(values)) != len(values):
                raise ValueError(f"{name} no puede contener duplicados")

        return self

    def combinations(self) -> tuple[tuple[Decimal, Decimal], ...]:
        """Pares (multiplicador SL, exposición) en el orden de la rejilla."""
        return tuple(product(self.stop_loss_atr_multiples, self.max_position_exposures))


class SensitivityRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_loss_atr_multiple: Decimal
    max_position_exposure: Decimal
    run_id: str | None
    cumulative_return: Decimal
    max_drawdown_pct: Decimal
    win_rate: Decimal | None
    profit_factor: Decimal | None
    expectancy_r: Decimal | None
    total_trades: int
    final_equity: Decimal
    total_costs: Decimal


class SensitivityReport(BaseModel):
    """Resultado de un barrido; se puede reconstruir desde `backtest_runs`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sweep_id: str | None
    symbols: tuple[str, ...]
    start: str | None
    end: str | None
    trading_calendar: str | None
    base_parameters: BacktestParameters
    grid: ParameterGrid
    buy_and_hold_return: Decimal | None
    rows: tuple[SensitivityRow, ...]

    def best_by_return(self) -> SensitivityRow:
        return max(self.rows, key=lambda row: row.cumulative_return)

    def best_by_drawdown(self) -> SensitivityRow:
        return min(self.rows, key=lambda row: row.max_drawdown_pct)


class SensitivityAnalysisService:
    """
    Barrido de parámetros sobre los datos de MySQL.

    Lee barras, dividendos y sesiones una sola vez y ejecuta un backtest por
    combinación de la rejilla, variando sólo `risk.stop_loss_atr_multiple` y
    `risk.max_position_exposure` sobre los parámetros base. Con `persist`,
    cada ejecución se guarda en `backtest_runs` con el mismo `sweep_id`.
    """

    def __init__(
        self,
        run_service: BacktestRunService,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._run_service = run_service
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    def run(
        self,
        request: BacktestRunRequest,
        grid: ParameterGrid,
        persist: bool = True,
    ) -> SensitivityReport:
        data = self._run_service.prepare(request)
        sweep_id = self._id_factory() if persist else None
        rows: list[SensitivityRow] = []
        buy_and_hold_return: Decimal | None = None
        first_report = None

        for stop_loss_multiple, exposure in grid.combinations():
            parameters = request.parameters.model_copy(
                update={
                    "risk": request.parameters.risk.model_copy(
                        update={
                            "stop_loss_atr_multiple": stop_loss_multiple,
                            "max_position_exposure": exposure,
                        }
                    )
                }
            )
            outcome = self._run_service.run_prepared(
                request.model_copy(update={"parameters": parameters}),
                data,
                persist=persist,
                sweep_id=sweep_id,
            )
            report = outcome.report
            metrics = report.metrics
            first_report = first_report or report

            if metrics.buy_and_hold is not None:
                buy_and_hold_return = metrics.buy_and_hold.cumulative_return

            rows.append(
                SensitivityRow(
                    stop_loss_atr_multiple=stop_loss_multiple,
                    max_position_exposure=exposure,
                    run_id=outcome.run_id,
                    cumulative_return=metrics.cumulative_return,
                    max_drawdown_pct=metrics.max_drawdown.max_drawdown_pct,
                    win_rate=metrics.trades.win_rate,
                    profit_factor=metrics.trades.profit_factor,
                    expectancy_r=metrics.trades.expectancy_r,
                    total_trades=metrics.trades.total_trades,
                    final_equity=metrics.final_equity,
                    total_costs=metrics.trades.total_costs,
                )
            )

        if first_report is None:
            raise ValueError("La rejilla de parámetros no tiene combinaciones")

        return SensitivityReport(
            sweep_id=sweep_id,
            symbols=first_report.symbols,
            start=first_report.start,
            end=first_report.end,
            trading_calendar=first_report.trading_calendar,
            base_parameters=request.parameters,
            grid=grid,
            buy_and_hold_return=buy_and_hold_return,
            rows=tuple(rows),
        )


def sensitivity_table(rows: Sequence[SensitivityRow]) -> list[str]:
    """Tabla Markdown (legible también en consola) con una fila por combinación."""
    lines = [
        "| SL × ATR | Exposición | Retorno | MDD | Win Rate | Profit Factor "
        "| Expectancy R | Trades | run_id |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    lines.extend(
        f"| {row.stop_loss_atr_multiple} | {_pct(row.max_position_exposure)} "
        f"| {_pct(row.cumulative_return)} | {_pct(row.max_drawdown_pct)} "
        f"| {_pct(row.win_rate)} | {_ratio(row.profit_factor)} "
        f"| {_ratio(row.expectancy_r)} | {row.total_trades} "
        f"| {row.run_id or NOT_AVAILABLE} |"
        for row in rows
    )
    return lines


def sensitivity_to_markdown(report: SensitivityReport) -> str:
    parameters = report.base_parameters
    best_return = report.best_by_return()
    best_drawdown = report.best_by_drawdown()

    lines = [
        "# Análisis de sensibilidad",
        "",
        f"- Barrido: `{report.sweep_id or 'no guardado'}`",
        f"- Símbolos: {', '.join(report.symbols)}",
        f"- Periodo: {report.start or NOT_AVAILABLE} → {report.end or NOT_AVAILABLE}",
        f"- Calendario de mercado: {report.trading_calendar or 'sin calendario'}",
        f"- Perfil de costes: {parameters.cost_profile or NOT_AVAILABLE} "
        f"(comisión {_pct(parameters.costs.commission_rate)}, "
        f"slippage {_pct(parameters.costs.slippage_rate)})",
        f"- Fijos: TP {parameters.risk.take_profit_atr_multiple} × ATR, "
        f"riesgo {_pct(parameters.risk.risk_per_trade)} por operación, "
        f"{parameters.risk.max_positions} posiciones, "
        f"anti-gap {parameters.gap_threshold_atr} × ATR",
        f"- Buy & Hold del periodo: {_pct(report.buy_and_hold_return)}",
        "",
        *sensitivity_table(report.rows),
        "",
        f"- Mayor retorno: SL {best_return.stop_loss_atr_multiple} × ATR, "
        f"exposición {_pct(best_return.max_position_exposure)} "
        f"({_pct(best_return.cumulative_return)}).",
        f"- Menor drawdown: SL {best_drawdown.stop_loss_atr_multiple} × ATR, "
        f"exposición {_pct(best_drawdown.max_position_exposure)} "
        f"({_pct(best_drawdown.max_drawdown_pct)}).",
        "",
    ]
    return "\n".join(lines)


def write_sensitivity_report(
    report: SensitivityReport,
    directory: Path,
    stem: str,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(sensitivity_to_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _pct(value: Decimal | None) -> str:
    if value is None:
        return NOT_AVAILABLE

    percentage = (value * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return f"{percentage} %"


def _ratio(value: Decimal | None) -> str:
    if value is None:
        return NOT_AVAILABLE

    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))
