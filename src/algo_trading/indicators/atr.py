from collections.abc import Sequence
from decimal import Decimal, localcontext

from algo_trading.indicators.price_series import (
    INDICATOR_PRECISION,
    INDICATOR_ROUNDING,
    PriceBar,
    validate_price_series,
    validate_window,
)


def true_ranges(bars: Sequence[PriceBar]) -> tuple[Decimal | None, ...]:
    """
    Calcula el True Range de cada barra.

    `TR[t] = max(High[t] - Low[t], |High[t] - Close[t-1]|, |Low[t] - Close[t-1]|)`.

    La primera barra no tiene cierre previo y su TR es None.
    """
    validate_price_series(bars)

    result: list[Decimal | None] = []

    for index, bar in enumerate(bars):
        if index == 0:
            result.append(None)
            continue

        previous_close = bars[index - 1].close
        result.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )

    return tuple(result)


def wilder_atr(
    bars: Sequence[PriceBar],
    window: int,
) -> tuple[Decimal | None, ...]:
    """
    Calcula el ATR con suavizado de Wilder (RMA).

    - Semilla: `ATR[window] = media(TR[1..window])`.
    - Después: `ATR[t] = (ATR[t-1] * (window - 1) + TR[t]) / window`.

    Con `window = 20`, el primer ATR está disponible en el índice 20, es
    decir, tras 21 barras. El valor en `t` sólo usa barras `<= t`.
    """
    validate_window(window)

    ranges = true_ranges(bars)
    result: list[Decimal | None] = []
    previous_atr: Decimal | None = None

    with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
        for index, true_range in enumerate(ranges):
            if index < window or true_range is None:
                result.append(None)
                continue

            if previous_atr is None:
                seed_ranges = [
                    value for value in ranges[1 : window + 1] if value is not None
                ]
                previous_atr = sum(seed_ranges, Decimal("0")) / window
            else:
                previous_atr = (previous_atr * (window - 1) + true_range) / window

            result.append(previous_atr)

    return tuple(result)
