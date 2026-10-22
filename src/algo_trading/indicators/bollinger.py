from collections.abc import Sequence
from decimal import Decimal, localcontext

from pydantic import BaseModel, ConfigDict

from algo_trading.indicators.moving_average import simple_moving_average
from algo_trading.indicators.price_series import (
    INDICATOR_PRECISION,
    INDICATOR_ROUNDING,
    validate_window,
)


class BollingerBand(BaseModel):
    """Bandas de Bollinger en una fecha concreta."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    middle: Decimal
    upper: Decimal
    lower: Decimal
    standard_deviation: Decimal


def rolling_population_std(
    values: Sequence[Decimal],
    window: int,
) -> tuple[Decimal | None, ...]:
    """
    Desviación estándar móvil poblacional (`ddof=0`).

    El valor en `t` usa únicamente `values[t-window+1 .. t]`.
    """
    validate_window(window)

    means = simple_moving_average(values, window)
    result: list[Decimal | None] = []

    with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
        for index, mean in enumerate(means):
            if mean is None:
                result.append(None)
                continue

            window_values = values[index + 1 - window : index + 1]
            squared_deviations = sum(
                ((value - mean) ** 2 for value in window_values),
                Decimal("0"),
            )
            result.append((squared_deviations / window).sqrt())

    return tuple(result)


def bollinger_bands(
    closes: Sequence[Decimal],
    window: int,
    std_multiplier: Decimal,
) -> tuple[BollingerBand | None, ...]:
    """
    Calcula SMA ± `std_multiplier` desviaciones estándar poblacionales.

    Las primeras `window - 1` posiciones son None (warm-up).
    """
    if std_multiplier <= 0:
        raise ValueError("std_multiplier debe ser mayor que 0")

    means = simple_moving_average(closes, window)
    deviations = rolling_population_std(closes, window)
    result: list[BollingerBand | None] = []

    with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
        for mean, deviation in zip(means, deviations, strict=True):
            if mean is None or deviation is None:
                result.append(None)
                continue

            width = std_multiplier * deviation
            result.append(
                BollingerBand(
                    middle=mean,
                    upper=mean + width,
                    lower=mean - width,
                    standard_deviation=deviation,
                )
            )

    return tuple(result)
