from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.indicators.atr import wilder_atr
from algo_trading.indicators.bollinger import bollinger_bands
from algo_trading.indicators.price_series import PriceBar, validate_price_series


class IndicatorParameters(BaseModel):
    """Parámetros de indicadores; los valores por defecto son los de la v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bollinger_window: int = Field(default=20, ge=2)
    bollinger_std_multiplier: Decimal = Field(default=Decimal("2"), gt=0)
    atr_window: int = Field(default=20, ge=1)


class IndicatorRow(BaseModel):
    """
    Indicadores disponibles al cierre de la barra `timestamp`.

    Cada valor se calcula sólo con barras cuyo timestamp es menor o igual
    que `timestamp`. Durante el warm-up los indicadores son None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timestamp: datetime
    close: Decimal
    sma: Decimal | None
    upper_band: Decimal | None
    lower_band: Decimal | None
    atr: Decimal | None


def calculate_indicators(
    bars: Sequence[PriceBar],
    parameters: IndicatorParameters | None = None,
) -> tuple[IndicatorRow, ...]:
    """
    Calcula SMA, Bandas de Bollinger y ATR para una serie de un símbolo.

    Función pura: no modifica las barras ni accede a persistencia.
    """
    validate_price_series(bars)
    parameters = parameters or IndicatorParameters()

    closes = [bar.close for bar in bars]
    bands = bollinger_bands(
        closes,
        window=parameters.bollinger_window,
        std_multiplier=parameters.bollinger_std_multiplier,
    )
    atrs = wilder_atr(bars, window=parameters.atr_window)

    return tuple(
        IndicatorRow(
            symbol=bar.symbol,
            timestamp=bar.timestamp,
            close=bar.close,
            sma=band.middle if band is not None else None,
            upper_band=band.upper if band is not None else None,
            lower_band=band.lower if band is not None else None,
            atr=atr,
        )
        for bar, band, atr in zip(bars, bands, atrs, strict=True)
    )
