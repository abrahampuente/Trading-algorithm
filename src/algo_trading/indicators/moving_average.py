from collections.abc import Sequence
from decimal import Decimal, localcontext

from algo_trading.indicators.price_series import (
    INDICATOR_PRECISION,
    INDICATOR_ROUNDING,
    validate_window,
)


def simple_moving_average(
    values: Sequence[Decimal],
    window: int,
) -> tuple[Decimal | None, ...]:
    """
    Calcula la SMA móvil.

    El valor en `t` usa únicamente `values[t-window+1 .. t]`. Las primeras
    `window - 1` posiciones son None (warm-up).
    """
    validate_window(window)

    result: list[Decimal | None] = []

    with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
        for index in range(len(values)):
            if index + 1 < window:
                result.append(None)
                continue

            window_values = values[index + 1 - window : index + 1]
            result.append(sum(window_values, Decimal("0")) / window)

    return tuple(result)
