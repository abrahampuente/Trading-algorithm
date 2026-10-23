"""
Filtros de régimen y de fuerza relativa para señales multi-factor.

Cada filtro evalúa una señal en su `t` con el historial del símbolo hasta
`t` y una vista del contexto de referencia hasta `t` (`SignalContext.until`).
Si faltan datos, el filtro no deja pasar la señal e indica el motivo.
"""

from collections.abc import Sequence
from decimal import Decimal, localcontext
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algo_trading.indicators.atr import wilder_atr
from algo_trading.indicators.price_series import (
    INDICATOR_PRECISION,
    INDICATOR_ROUNDING,
    PriceBar,
)
from algo_trading.signals.base import Signal
from algo_trading.signals.context import SignalContext
from algo_trading.signals.factors import percentile_rank, relative_strength

INSUFFICIENT_HISTORY = "insufficient_history"
MISSING_REFERENCE_DATA = "missing_reference_data"
UNMAPPED_SECTOR = "unmapped_sector"


class FilterResult(BaseModel):
    """Resultado de un filtro: si la señal pasa y los valores que se usaron."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    passed: bool
    values: dict[str, Decimal] = Field(default_factory=dict)
    reason: str | None = None


class SignalFilter(Protocol):
    """
    Filtro de una señal en su `t`.

    `bars` es el historial del símbolo hasta `t` inclusive (la última barra
    es la de la señal) y `context` sólo contiene barras con timestamp `<= t`.
    """

    @property
    def name(self) -> str: ...

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult: ...


class RelativeStrengthFilter(BaseModel):
    """
    Exige fuerza relativa del símbolo frente a un benchmark (SPY o QQQ).

    `RS = (Close[t] / Close[t-n]) / (Bench[t] / Bench[t-n]) - 1 >= mínimo`.
    El benchmark debe tener barra en cada sesión de la ventana.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark: str = "SPY"
    lookback: int = Field(default=63, ge=1)
    min_relative_strength: Decimal = Decimal("0")

    @property
    def name(self) -> str:
        return f"relative_strength:{self.benchmark}"

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult:
        value = _aligned_relative_strength(bars, self.benchmark, context, self.lookback)

        if isinstance(value, str):
            return FilterResult(name=self.name, passed=False, reason=value)

        return FilterResult(
            name=self.name,
            passed=value >= self.min_relative_strength,
            values={"relative_strength": value},
        )


class SectorRotationFilter(BaseModel):
    """
    Rotación sectorial: sólo deja pasar símbolos de los sectores más fuertes.

    En `t` ordena los sectores por la fuerza relativa de su ETF frente al
    benchmark (`lookback` sesiones; empate por nombre del sector) y exige que
    el sector del símbolo esté entre los `top_k` primeros.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark: str = "SPY"
    sector_map: dict[str, str] = Field(min_length=1)
    sector_etfs: dict[str, str] = Field(min_length=1)
    lookback: int = Field(default=63, ge=1)
    top_k: int = Field(default=4, ge=1)

    @model_validator(mode="after")
    def validate_sectors(self) -> Self:
        unknown = sorted(set(self.sector_map.values()) - set(self.sector_etfs))

        if unknown:
            raise ValueError("Sectores sin ETF en sector_etfs: " + ", ".join(unknown))

        return self

    @property
    def name(self) -> str:
        return f"sector_rotation:{self.benchmark}"

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult:
        sector = self.sector_map.get(signal.symbol)

        if sector is None:
            return FilterResult(name=self.name, passed=False, reason=UNMAPPED_SECTOR)

        strengths: dict[str, Decimal] = {}

        for sector_name, etf in sorted(self.sector_etfs.items()):
            etf_bars = context.bars(etf)

            if not etf_bars or etf_bars[-1].timestamp != signal.signal_timestamp:
                return FilterResult(
                    name=self.name, passed=False, reason=MISSING_REFERENCE_DATA
                )

            value = _aligned_relative_strength(
                etf_bars, self.benchmark, context, self.lookback
            )

            if isinstance(value, str):
                return FilterResult(name=self.name, passed=False, reason=value)

            strengths[sector_name] = value

        ranking = sorted(strengths, key=lambda name: (-strengths[name], name))
        rank = ranking.index(sector) + 1

        return FilterResult(
            name=self.name,
            passed=rank <= self.top_k,
            values={
                "sector_relative_strength": strengths[sector],
                "sector_rank": Decimal(rank),
                "sectors_ranked": Decimal(len(ranking)),
            },
        )


class VolatilityRegimeFilter(BaseModel):
    """
    Filtro de régimen por volatilidad propia: ATR relativo (`ATR / Close`) en
    `t` con percentil `<= max_percentile` dentro de sus últimas `lookback`
    observaciones.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    atr_window: int = Field(default=20, ge=1)
    lookback: int = Field(default=252, ge=2)
    max_percentile: Decimal = Field(default=Decimal("0.9"), ge=0, le=1)

    @property
    def name(self) -> str:
        return "atr_regime"

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult:
        atrs = wilder_atr(bars, window=self.atr_window)

        with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
            ratios = [
                atr / bar.close
                for bar, atr in zip(bars, atrs, strict=True)
                if atr is not None
            ][-self.lookback :]

        if len(ratios) < self.lookback:
            return FilterResult(
                name=self.name, passed=False, reason=INSUFFICIENT_HISTORY
            )

        percentile = percentile_rank(ratios)
        assert percentile is not None

        return FilterResult(
            name=self.name,
            passed=percentile <= self.max_percentile,
            values={"atr_to_close": ratios[-1], "atr_percentile": percentile},
        )


class VixRegimeFilter(BaseModel):
    """Régimen de mercado: cierre del VIX en `t` `<= max_level`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = "^VIX"
    max_level: Decimal = Field(default=Decimal("25"), gt=0)

    @property
    def name(self) -> str:
        return "vix_regime"

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult:
        level = context.close_at(self.symbol, signal.signal_timestamp)

        if level is None:
            return FilterResult(
                name=self.name, passed=False, reason=MISSING_REFERENCE_DATA
            )

        return FilterResult(
            name=self.name,
            passed=level <= self.max_level,
            values={"vix": level},
        )


class BenchmarkTrendFilter(BaseModel):
    """Régimen de tendencia: `Close_bench[t] > SMA(Close_bench, window)[t]`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    benchmark: str = "SPY"
    window: int = Field(default=200, ge=2)

    @property
    def name(self) -> str:
        return f"trend:{self.benchmark}"

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult:
        benchmark_bars = context.bars(self.benchmark)

        if (
            not benchmark_bars
            or benchmark_bars[-1].timestamp != signal.signal_timestamp
        ):
            return FilterResult(
                name=self.name, passed=False, reason=MISSING_REFERENCE_DATA
            )

        if len(benchmark_bars) < self.window:
            return FilterResult(
                name=self.name, passed=False, reason=INSUFFICIENT_HISTORY
            )

        closes = [bar.close for bar in benchmark_bars[-self.window :]]

        with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
            sma = sum(closes, Decimal("0")) / self.window

        return FilterResult(
            name=self.name,
            passed=closes[-1] > sma,
            values={"benchmark_close": closes[-1], "benchmark_sma": sma},
        )


def _aligned_relative_strength(
    bars: Sequence[PriceBar],
    benchmark: str,
    context: SignalContext,
    lookback: int,
) -> Decimal | str:
    """
    Fuerza relativa en la última barra de `bars` frente a `benchmark`.

    Devuelve el motivo de rechazo si falta historial o alguna barra del
    benchmark en las sesiones de la ventana.
    """
    if len(bars) <= lookback:
        return INSUFFICIENT_HISTORY

    window = bars[-1 - lookback :]
    benchmark_closes: list[Decimal] = []

    for bar in window:
        close = context.close_at(benchmark, bar.timestamp)

        if close is None:
            return MISSING_REFERENCE_DATA

        benchmark_closes.append(close)

    value = relative_strength([bar.close for bar in window], benchmark_closes, lookback)
    assert value is not None
    return value
