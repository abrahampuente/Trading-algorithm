"""
Factores puntuales evaluados en la última posición de cada secuencia.

Funciones puras en `Decimal`: reciben sólo el historial hasta `t`.
"""

from collections.abc import Sequence
from decimal import Decimal, localcontext

from algo_trading.indicators.price_series import (
    INDICATOR_PRECISION,
    INDICATOR_ROUNDING,
    validate_window,
)


def relative_strength(
    closes: Sequence[Decimal],
    benchmark_closes: Sequence[Decimal],
    lookback: int,
) -> Decimal | None:
    """
    Fuerza relativa frente a un benchmark en la última posición `t`.

    `RS = (Close[t] / Close[t-n]) / (Bench[t] / Bench[t-n]) - 1`, con ambas
    secuencias alineadas por sesión. None si no hay `lookback + 1` valores.
    """
    validate_window(lookback)

    if len(closes) != len(benchmark_closes):
        raise ValueError("closes y benchmark_closes deben estar alineados")

    if len(closes) <= lookback:
        return None

    with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
        own_return = closes[-1] / closes[-1 - lookback]
        benchmark_return = benchmark_closes[-1] / benchmark_closes[-1 - lookback]
        return own_return / benchmark_return - 1


def percentile_rank(values: Sequence[Decimal]) -> Decimal | None:
    """
    Percentil del último valor dentro de `values` (incluido él mismo).

    Fracción de valores menores o iguales que el último: 1 si es el máximo.
    None si `values` está vacío.
    """
    if not values:
        return None

    current = values[-1]
    at_or_below = sum(1 for value in values if value <= current)

    with localcontext(prec=INDICATOR_PRECISION, rounding=INDICATOR_ROUNDING):
        return Decimal(at_or_below) / len(values)
