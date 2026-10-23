"""
Walk-Forward Optimization (WFO).

Para cada ventana rodante:

1. In-Sample (IS): ejecuta la rejilla de parámetros y elige la combinación
   con mejor métrica (Profit Factor o Sharpe).
2. Out-of-Sample (OOS): aplica esa combinación, sin cambios, a la ventana
   inmediatamente posterior.

Las ventanas OOS se encadenan (cada una empieza con el equity final de la
anterior) para formar una curva de equity fuera de muestra.

Garantías contra el sesgo:

- Cada ejecución recibe sólo barras hasta el fin de su ventana, de modo que
  ni la selección IS ni la ejecución OOS ven datos posteriores.
- Las barras anteriores a la ventana sólo calientan los indicadores
  (`trading_start`): siempre se usa el historial desde el inicio de los
  datos, por lo que los indicadores son idénticos en todas las ventanas.
- Las posiciones abiertas al final de una ventana se cierran al último cierre
  de la ventana (`END_OF_DATA`), de modo que cada ventana es independiente.
"""

import calendar
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algo_trading.analytics.metrics import (
    calculate_max_drawdown,
    calculate_trade_statistics,
)
from algo_trading.backtesting.engine import (
    BacktestEngine,
    BacktestParameters,
    EquityPoint,
    ExecutionBar,
)
from algo_trading.backtesting.sensitivity import ParameterGrid
from algo_trading.domain.corporate_actions.dto import CorporateDividend
from algo_trading.execution.trade import Trade
from algo_trading.portfolio.position_sizing import MONEY_PRECISION, MONEY_ROUNDING

TRADING_DAYS_PER_YEAR = 252
DAYS_PER_YEAR = Decimal("365.25")
ROBUST_WFE_THRESHOLD = Decimal("0.5")
NOT_AVAILABLE = "n/d"


class SelectionMetric(StrEnum):
    PROFIT_FACTOR = "profit_factor"
    SHARPE = "sharpe"


class WalkForwardConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    in_sample_months: int = Field(default=36, ge=1)
    out_of_sample_months: int = Field(default=12, ge=1)
    step_months: int | None = Field(
        default=None,
        ge=1,
        description="Desplazamiento entre ventanas; por defecto, out_of_sample.",
    )
    selection_metric: SelectionMetric = SelectionMetric.PROFIT_FACTOR
    min_trades: int = Field(
        default=10,
        ge=0,
        description="Mínimo de trades IS para que una combinación sea elegible.",
    )
    grid: ParameterGrid = ParameterGrid()

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        # Con un paso menor, las OOS se solaparían y la curva encadenada
        # contaría dos veces los mismos días.
        if self.step_months is not None and self.step_months < (
            self.out_of_sample_months
        ):
            raise ValueError("step_months no puede ser menor que out_of_sample_months")

        return self

    @property
    def effective_step_months(self) -> int:
        return self.step_months or self.out_of_sample_months


class WalkForwardWindow(BaseModel):
    """Fechas inclusivas de una ventana IS y su ventana OOS posterior."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=1)
    in_sample_start: date
    in_sample_end: date
    out_of_sample_start: date
    out_of_sample_end: date
    is_partial: bool = False

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if not (
            self.in_sample_start
            <= self.in_sample_end
            < self.out_of_sample_start
            <= self.out_of_sample_end
        ):
            raise ValueError("Las ventanas IS y OOS deben ser consecutivas")

        return self


class PeriodEvaluation(BaseModel):
    """Resultado de una combinación de parámetros en un periodo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stop_loss_atr_multiple: Decimal
    max_position_exposure: Decimal
    initial_equity: Decimal
    final_equity: Decimal
    cumulative_return: Decimal
    annualized_return: Decimal | None
    max_drawdown_pct: Decimal
    profit_factor: Decimal | None
    sharpe_ratio: Decimal | None
    win_rate: Decimal | None
    total_trades: int = Field(ge=0)


class WalkForwardWindowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: WalkForwardWindow
    in_sample: tuple[PeriodEvaluation, ...]
    selected: PeriodEvaluation
    out_of_sample: PeriodEvaluation
    walk_forward_efficiency: Decimal | None


class WalkForwardReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report_version: str = "walk-forward-report-v1"
    symbols: tuple[str, ...]
    trading_calendar: str | None
    base_parameters: BacktestParameters
    config: WalkForwardConfig
    windows: tuple[WalkForwardWindowResult, ...]
    out_of_sample: PeriodEvaluation
    out_of_sample_trades: tuple[Trade, ...]
    out_of_sample_equity_curve: tuple[EquityPoint, ...]
    mean_in_sample_annualized_return: Decimal | None
    walk_forward_efficiency: Decimal | None

    @property
    def is_robust(self) -> bool | None:
        """WFE > 50 %: el rendimiento OOS conserva más de la mitad del IS."""
        if self.walk_forward_efficiency is None:
            return None

        return self.walk_forward_efficiency > ROBUST_WFE_THRESHOLD


def add_months(value: date, months: int) -> date:
    """Suma meses ajustando el día al último del mes si no existe."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def build_windows(
    start: date,
    end: date,
    in_sample_months: int,
    out_of_sample_months: int,
    step_months: int | None = None,
) -> tuple[WalkForwardWindow, ...]:
    """
    Ventanas rodantes: IS de `in_sample_months` seguido de OOS de
    `out_of_sample_months`; cada ventana se desplaza `step_months` (por
    defecto, la longitud OOS, de modo que las OOS no se solapan). La última
    OOS se recorta a `end` y se marca como parcial.
    """
    step = step_months or out_of_sample_months
    windows: list[WalkForwardWindow] = []
    window_start = start

    while True:
        out_of_sample_start = add_months(window_start, in_sample_months)

        if out_of_sample_start > end:
            break

        full_end = date.fromordinal(
            add_months(out_of_sample_start, out_of_sample_months).toordinal() - 1
        )
        windows.append(
            WalkForwardWindow(
                index=len(windows) + 1,
                in_sample_start=window_start,
                in_sample_end=date.fromordinal(out_of_sample_start.toordinal() - 1),
                out_of_sample_start=out_of_sample_start,
                out_of_sample_end=min(full_end, end),
                is_partial=full_end > end,
            )
        )
        window_start = add_months(window_start, step)

    return tuple(windows)


def annualized_return(
    cumulative_return: Decimal,
    start: date,
    end: date,
) -> Decimal | None:
    """`(1 + R) ^ (365.25 / días) - 1`, con días naturales inclusivos."""
    days = (end - start).days + 1
    growth = 1 + cumulative_return

    if days <= 0 or growth <= 0:
        return None

    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        return growth ** (DAYS_PER_YEAR / days) - 1


def sharpe_ratio(
    equity_curve: Sequence[EquityPoint],
    initial_equity: Decimal,
) -> Decimal | None:
    """
    Sharpe anualizado de los retornos diarios de la curva (tasa libre 0).

    `media / desviación muestral * sqrt(252)`; None con menos de dos
    retornos o volatilidad nula.
    """
    equities = [initial_equity, *(point.equity for point in equity_curve)]

    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        returns = [
            current / previous - 1
            for previous, current in zip(equities, equities[1:], strict=False)
            if previous > 0
        ]

        if len(returns) < 2:
            return None

        mean = sum(returns, Decimal("0")) / len(returns)
        variance = sum(((value - mean) ** 2 for value in returns), Decimal("0")) / (
            len(returns) - 1
        )

        if variance == 0:
            return None

        return mean / variance.sqrt() * Decimal(TRADING_DAYS_PER_YEAR).sqrt()


def select_best(
    evaluations: Sequence[PeriodEvaluation],
    metric: SelectionMetric,
    min_trades: int,
) -> PeriodEvaluation:
    """
    Elige la mejor combinación IS.

    - Sólo son elegibles las combinaciones con al menos `min_trades` trades;
      si ninguna llega, las que tienen algún trade; si tampoco, todas.
    - Profit Factor sin pérdidas (None con trades) cuenta como infinito.
    - Empates: gana la primera en el orden de la rejilla.
    """
    if not evaluations:
        raise ValueError("No hay combinaciones que evaluar")

    eligible = [e for e in evaluations if e.total_trades >= min_trades] or [
        e for e in evaluations if e.total_trades > 0
    ]
    candidates = eligible or list(evaluations)

    def score(evaluation: PeriodEvaluation) -> Decimal:
        if metric is SelectionMetric.PROFIT_FACTOR:
            if evaluation.profit_factor is None:
                return (
                    Decimal("Infinity")
                    if evaluation.total_trades > 0
                    else Decimal("-Infinity")
                )
            return evaluation.profit_factor

        return (
            evaluation.sharpe_ratio
            if evaluation.sharpe_ratio is not None
            else Decimal("-Infinity")
        )

    best = candidates[0]

    for evaluation in candidates[1:]:
        if score(evaluation) > score(best):
            best = evaluation

    return best


class WalkForwardAnalyzer:
    """
    Ejecuta el walk-forward sobre datos ya cargados (sin acceso a MySQL).

    `bars_by_symbol` debe contener el historial completo desde el inicio del
    análisis; cada ventana usa sólo las barras hasta su fin.
    """

    def __init__(
        self,
        bars_by_symbol: Mapping[str, Sequence[ExecutionBar]],
        dividends_by_symbol: Mapping[str, Sequence[CorporateDividend]] | None = None,
        trading_sessions: Sequence[date] | None = None,
        trading_calendar: str | None = None,
    ) -> None:
        if not bars_by_symbol:
            raise ValueError("No hay barras para el walk-forward")

        self._bars = {symbol: tuple(bars) for symbol, bars in bars_by_symbol.items()}
        self._dividends = {
            symbol: tuple(dividends)
            for symbol, dividends in (dividends_by_symbol or {}).items()
        }
        self._sessions = tuple(trading_sessions) if trading_sessions else None
        self._calendar_name = trading_calendar

    @property
    def data_start(self) -> date:
        return min(bars[0].timestamp.date() for bars in self._bars.values() if bars)

    @property
    def data_end(self) -> date:
        return max(bars[-1].timestamp.date() for bars in self._bars.values() if bars)

    def evaluate(
        self,
        parameters: BacktestParameters,
        start: date,
        end: date,
    ) -> tuple[PeriodEvaluation, tuple[Trade, ...], tuple[EquityPoint, ...]]:
        """Backtest con entradas sólo en [start, end] y datos hasta `end`."""
        bars = {
            symbol: tuple(bar for bar in series if bar.timestamp.date() <= end)
            for symbol, series in self._bars.items()
        }
        bars = {symbol: series for symbol, series in bars.items() if series}
        dividends = {
            symbol: tuple(d for d in values if d.ex_date <= end)
            for symbol, values in self._dividends.items()
        }
        sessions = (
            tuple(session for session in self._sessions if session <= end)
            if self._sessions is not None
            else None
        )

        result = BacktestEngine(parameters).run(
            bars,
            trading_sessions=sessions,
            dividends_by_symbol=dividends,
            trading_start=start,
        )
        initial_equity = parameters.initial_cash

        with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
            cumulative_return = result.final_cash / initial_equity - 1

        statistics = calculate_trade_statistics(result.trades)
        curve = result.equity_curve
        last_date = curve[-1].timestamp.date() if curve else end

        evaluation = PeriodEvaluation(
            stop_loss_atr_multiple=parameters.risk.stop_loss_atr_multiple,
            max_position_exposure=parameters.risk.max_position_exposure,
            initial_equity=initial_equity,
            final_equity=result.final_cash,
            cumulative_return=cumulative_return,
            annualized_return=annualized_return(cumulative_return, start, last_date),
            max_drawdown_pct=calculate_max_drawdown(
                [(point.timestamp, point.equity) for point in curve],
                initial_equity,
            ).max_drawdown_pct,
            profit_factor=statistics.profit_factor,
            sharpe_ratio=sharpe_ratio(curve, initial_equity),
            win_rate=statistics.win_rate,
            total_trades=statistics.total_trades,
        )
        return evaluation, result.trades, curve

    def run(
        self,
        base_parameters: BacktestParameters,
        config: WalkForwardConfig,
        start: date | None = None,
        end: date | None = None,
    ) -> WalkForwardReport:
        analysis_start = start or self.data_start
        analysis_end = end or self.data_end
        windows = build_windows(
            analysis_start,
            analysis_end,
            config.in_sample_months,
            config.out_of_sample_months,
            config.effective_step_months,
        )

        if not windows:
            raise ValueError(
                "El periodo es demasiado corto para una ventana IS de "
                f"{config.in_sample_months} meses seguida de OOS"
            )

        oos_equity = base_parameters.initial_cash
        window_results: list[WalkForwardWindowResult] = []
        oos_trades: list[Trade] = []
        oos_curve: list[EquityPoint] = []

        for window in windows:
            in_sample = tuple(
                self.evaluate(
                    _with_risk(base_parameters, stop_loss, exposure),
                    window.in_sample_start,
                    window.in_sample_end,
                )[0]
                for stop_loss, exposure in config.grid.combinations()
            )
            selected = select_best(
                in_sample,
                config.selection_metric,
                config.min_trades,
            )
            oos_parameters = _with_risk(
                base_parameters,
                selected.stop_loss_atr_multiple,
                selected.max_position_exposure,
            ).model_copy(update={"initial_cash": oos_equity})
            out_of_sample, trades, curve = self.evaluate(
                oos_parameters,
                window.out_of_sample_start,
                window.out_of_sample_end,
            )
            oos_equity = out_of_sample.final_equity
            oos_trades.extend(trades)
            oos_curve.extend(curve)
            window_results.append(
                WalkForwardWindowResult(
                    window=window,
                    in_sample=in_sample,
                    selected=selected,
                    out_of_sample=out_of_sample,
                    walk_forward_efficiency=_efficiency(
                        out_of_sample.annualized_return,
                        selected.annualized_return,
                    ),
                )
            )

        initial_equity = base_parameters.initial_cash

        with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
            cumulative = oos_equity / initial_equity - 1
            in_sample_returns = [
                result.selected.annualized_return
                for result in window_results
                if result.selected.annualized_return is not None
            ]
            mean_in_sample = (
                sum(in_sample_returns, Decimal("0")) / len(in_sample_returns)
                if in_sample_returns
                else None
            )

        statistics = calculate_trade_statistics(oos_trades)
        first_oos = windows[0].out_of_sample_start
        last_oos = oos_curve[-1].timestamp.date() if oos_curve else analysis_end
        oos_annualized = annualized_return(cumulative, first_oos, last_oos)
        last_selected = window_results[-1].selected

        return WalkForwardReport(
            symbols=tuple(sorted(self._bars)),
            trading_calendar=self._calendar_name,
            base_parameters=base_parameters,
            config=config,
            windows=tuple(window_results),
            out_of_sample=PeriodEvaluation(
                stop_loss_atr_multiple=last_selected.stop_loss_atr_multiple,
                max_position_exposure=last_selected.max_position_exposure,
                initial_equity=initial_equity,
                final_equity=oos_equity,
                cumulative_return=cumulative,
                annualized_return=oos_annualized,
                max_drawdown_pct=calculate_max_drawdown(
                    [(point.timestamp, point.equity) for point in oos_curve],
                    initial_equity,
                ).max_drawdown_pct,
                profit_factor=statistics.profit_factor,
                sharpe_ratio=sharpe_ratio(oos_curve, initial_equity),
                win_rate=statistics.win_rate,
                total_trades=statistics.total_trades,
            ),
            out_of_sample_trades=tuple(oos_trades),
            out_of_sample_equity_curve=tuple(oos_curve),
            mean_in_sample_annualized_return=mean_in_sample,
            walk_forward_efficiency=_efficiency(oos_annualized, mean_in_sample),
        )


def walk_forward_to_markdown(report: WalkForwardReport) -> str:
    config = report.config
    parameters = report.base_parameters
    oos = report.out_of_sample
    first_window = report.windows[0].window
    last_window = report.windows[-1].window
    verdict = {
        True: "robusta (WFE > 50 %)",
        False: "no robusta (WFE <= 50 %)",
        None: "no calculable (retorno IS anualizado <= 0)",
    }[report.is_robust]

    lines = [
        "# Walk-Forward Optimization",
        "",
        f"- Símbolos: {', '.join(report.symbols)}",
        f"- Calendario de mercado: {report.trading_calendar or 'sin calendario'}",
        f"- Ventanas: IS {config.in_sample_months} meses, OOS "
        f"{config.out_of_sample_months} meses, paso "
        f"{config.effective_step_months} meses ({len(report.windows)} ventanas)",
        f"- Selección IS: mayor {_metric_label(config.selection_metric)}, "
        f"mínimo {config.min_trades} trades",
        "- Rejilla: SL × ATR "
        + ", ".join(str(value) for value in config.grid.stop_loss_atr_multiples)
        + "; exposición "
        + ", ".join(_pct(value) for value in config.grid.max_position_exposures),
        f"- Perfil de costes: {parameters.cost_profile or NOT_AVAILABLE}; "
        f"TP {parameters.risk.take_profit_atr_multiple} × ATR; anti-gap "
        f"{parameters.gap_threshold_atr} × ATR",
        f"- Periodo OOS: {first_window.out_of_sample_start} → "
        f"{last_window.out_of_sample_end}",
        "",
        "## Resultado fuera de muestra",
        "",
        "| Métrica | Valor |",
        "| --- | --- |",
        f"| Capital inicial | {_money(oos.initial_equity)} |",
        f"| Equity final OOS | {_money(oos.final_equity)} |",
        f"| Retorno acumulado OOS | {_pct(oos.cumulative_return)} |",
        f"| Retorno anualizado OOS | {_pct(oos.annualized_return)} |",
        f"| Retorno anualizado IS medio (seleccionados) | "
        f"{_pct(report.mean_in_sample_annualized_return)} |",
        f"| **Eficiencia Walk-Forward (WFE)** | "
        f"**{_pct(report.walk_forward_efficiency)}** |",
        f"| Maximum Drawdown OOS | {_pct(oos.max_drawdown_pct)} |",
        f"| Profit Factor OOS | {_ratio(oos.profit_factor)} |",
        f"| Sharpe OOS | {_ratio(oos.sharpe_ratio)} |",
        f"| Win Rate OOS | {_pct(oos.win_rate)} |",
        f"| Trades OOS | {oos.total_trades} |",
        "",
        f"Veredicto: {verdict}.",
        "",
        "## Ventanas",
        "",
        "| # | IS | OOS | SL × ATR | Exposición | PF IS | Sharpe IS "
        "| Anual. IS | Anual. OOS | Retorno OOS | MDD OOS | PF OOS | Trades OOS "
        "| WFE |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
        "| ---: | ---: | ---: | ---: |",
    ]

    for result in report.windows:
        window = result.window
        selected = result.selected
        out = result.out_of_sample
        partial = " (parcial)" if window.is_partial else ""
        lines.append(
            f"| {window.index} | {window.in_sample_start} → {window.in_sample_end} "
            f"| {window.out_of_sample_start} → {window.out_of_sample_end}{partial} "
            f"| {selected.stop_loss_atr_multiple} "
            f"| {_pct(selected.max_position_exposure)} "
            f"| {_ratio(selected.profit_factor)} | {_ratio(selected.sharpe_ratio)} "
            f"| {_pct(selected.annualized_return)} | {_pct(out.annualized_return)} "
            f"| {_pct(out.cumulative_return)} | {_pct(out.max_drawdown_pct)} "
            f"| {_ratio(out.profit_factor)} | {out.total_trades} "
            f"| {_pct(result.walk_forward_efficiency)} |"
        )

    lines.extend(["", "## Rejilla In-Sample por ventana", ""])

    for result in report.windows:
        window = result.window
        lines.extend(
            [
                f"### Ventana {window.index}: IS {window.in_sample_start} → "
                f"{window.in_sample_end}",
                "",
                "| SL × ATR | Exposición | Retorno | Anualizado | MDD | PF | Sharpe "
                "| Win Rate | Trades | Elegida |",
                "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: "
                "| :---: |",
            ]
        )
        lines.extend(
            f"| {e.stop_loss_atr_multiple} | {_pct(e.max_position_exposure)} "
            f"| {_pct(e.cumulative_return)} | {_pct(e.annualized_return)} "
            f"| {_pct(e.max_drawdown_pct)} | {_ratio(e.profit_factor)} "
            f"| {_ratio(e.sharpe_ratio)} | {_pct(e.win_rate)} | {e.total_trades} "
            f"| {'✓' if e == result.selected else ''} |"
            for e in result.in_sample
        )
        lines.append("")

    lines.extend(
        [
            "## Notas de método",
            "",
            "- Cada ejecución usa sólo barras hasta el fin de su ventana; las "
            "anteriores sólo calientan indicadores.",
            "- Las ventanas OOS se encadenan: cada una empieza con el equity "
            "final de la anterior.",
            "- Las posiciones abiertas al final de cada ventana se cierran al "
            "último cierre de la ventana.",
            "- WFE = retorno anualizado OOS / retorno anualizado IS. Por ventana "
            "usa la combinación elegida; el global usa la curva OOS encadenada y "
            "la media de los IS elegidos.",
            "",
        ]
    )
    return "\n".join(lines)


def write_walk_forward_report(
    report: WalkForwardReport,
    directory: Path,
    stem: str,
) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{stem}.json"
    markdown_path = directory / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    markdown_path.write_text(walk_forward_to_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _with_risk(
    parameters: BacktestParameters,
    stop_loss_atr_multiple: Decimal,
    max_position_exposure: Decimal,
) -> BacktestParameters:
    return parameters.model_copy(
        update={
            "risk": parameters.risk.model_copy(
                update={
                    "stop_loss_atr_multiple": stop_loss_atr_multiple,
                    "max_position_exposure": max_position_exposure,
                }
            )
        }
    )


def _efficiency(
    out_of_sample: Decimal | None,
    in_sample: Decimal | None,
) -> Decimal | None:
    """OOS / IS anualizados; no calculable si el IS no es positivo."""
    if out_of_sample is None or in_sample is None or in_sample <= 0:
        return None

    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        return out_of_sample / in_sample


def _metric_label(metric: SelectionMetric) -> str:
    return "Profit Factor" if metric is SelectionMetric.PROFIT_FACTOR else "Sharpe"


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
