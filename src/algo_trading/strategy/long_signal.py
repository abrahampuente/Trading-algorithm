from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algo_trading.indicators.indicator_frame import (
    IndicatorParameters,
    IndicatorRow,
    calculate_indicators,
)
from algo_trading.indicators.price_series import (
    INDICATOR_PRECISION,
    INDICATOR_ROUNDING,
    PriceBar,
)


class SignalParameters(BaseModel):
    """Parámetros de la señal Long; los valores por defecto son los de la v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    indicators: IndicatorParameters = IndicatorParameters()
    momentum_short_lookback: int = Field(default=10, ge=1)
    momentum_long_lookback: int = Field(default=20, ge=2)

    @model_validator(mode="after")
    def validate_lookbacks(self) -> Self:
        if self.momentum_short_lookback >= self.momentum_long_lookback:
            raise ValueError(
                "momentum_short_lookback debe ser menor que momentum_long_lookback"
            )

        return self


class LongSignal(BaseModel):
    """
    Señal Long emitida al cierre de `signal_timestamp`.

    La entrada no forma parte de la señal: se simula en la apertura de la
    siguiente sesión (`t+1`). Todos los campos se conocen al cierre de `t`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    signal_timestamp: datetime
    close: Decimal
    upper_band: Decimal
    previous_close: Decimal
    previous_upper_band: Decimal
    atr: Decimal = Field(gt=0)
    momentum: Decimal


class LongSignalGenerator:
    """
    Genera señales Long de momentum y ruptura de Bollinger.

    En la barra `t` se emite señal si:

    - `Close[t] > Close[t-10] > Close[t-20]`
    - `Close[t-1] <= UpperBand[t-1]`
    - `Close[t] > UpperBand[t]`

    `momentum = Close[t] / Close[t-20] - 1` se usa para priorizar señales
    simultáneas.
    """

    def __init__(self, parameters: SignalParameters | None = None) -> None:
        self._parameters = parameters or SignalParameters()

    def generate(self, bars: Sequence[PriceBar]) -> tuple[LongSignal, ...]:
        """Evalúa cada barra usando únicamente el historial hasta esa barra."""
        rows = calculate_indicators(bars, self._parameters.indicators)
        history_length = self._parameters.momentum_long_lookback + 1
        signals: list[LongSignal] = []

        for index in range(len(rows)):
            history = rows[max(0, index + 1 - history_length) : index + 1]
            signal = self.evaluate(history)

            if signal is not None:
                signals.append(signal)

        return tuple(signals)

    def evaluate(self, history: Sequence[IndicatorRow]) -> LongSignal | None:
        """
        Evalúa la señal en la última fila de `history`.

        Recibe sólo el pasado y el presente, de modo que no puede usar datos
        posteriores a `t`.
        """
        long_lookback = self._parameters.momentum_long_lookback
        short_lookback = self._parameters.momentum_short_lookback

        if len(history) <= long_lookback:
            return None

        current = history[-1]
        previous = history[-2]
        close_short = history[-1 - short_lookback].close
        close_long = history[-1 - long_lookback].close

        if (
            current.upper_band is None
            or previous.upper_band is None
            or current.atr is None
            or current.atr <= 0
        ):
            return None

        has_momentum = current.close > close_short > close_long
        was_inside_band = previous.close <= previous.upper_band
        breaks_upper_band = current.close > current.upper_band

        if not (has_momentum and was_inside_band and breaks_upper_band):
            return None

        with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
            momentum = current.close / close_long - 1

        return LongSignal(
            symbol=current.symbol,
            signal_timestamp=current.timestamp,
            close=current.close,
            upper_band=current.upper_band,
            previous_close=previous.close,
            previous_upper_band=previous.upper_band,
            atr=current.atr,
            momentum=momentum,
        )
