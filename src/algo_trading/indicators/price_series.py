from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Protocol

INDICATOR_PRECISION = 28
INDICATOR_ROUNDING = ROUND_HALF_EVEN


class PriceBar(Protocol):
    """
    Barra diaria mínima para calcular indicadores.

    La cumplen tanto `MarketBar` como `MarketBarAdjusted`. Los indicadores
    deben calcularse sobre precios ajustados.
    """

    @property
    def symbol(self) -> str: ...

    @property
    def timestamp(self) -> datetime: ...

    @property
    def high(self) -> Decimal: ...

    @property
    def low(self) -> Decimal: ...

    @property
    def close(self) -> Decimal: ...


def validate_price_series(bars: Sequence[PriceBar]) -> None:
    """Exige un único símbolo y timestamps estrictamente crecientes."""
    if not bars:
        return

    symbol = bars[0].symbol

    for previous_bar, bar in zip(bars, bars[1:], strict=False):
        if bar.symbol != symbol:
            raise ValueError("La serie de precios debe contener un único símbolo")

        if bar.timestamp <= previous_bar.timestamp:
            raise ValueError(
                "La serie de precios debe estar ordenada por timestamp "
                "estrictamente creciente"
            )


def validate_window(window: int) -> None:
    if window < 1:
        raise ValueError("window debe ser mayor o igual que 1")
