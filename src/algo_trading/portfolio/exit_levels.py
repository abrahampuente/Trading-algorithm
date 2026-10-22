from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ExitLevels(BaseModel):
    """Niveles fijos de salida de una posición Long; no se recalculan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_price: Decimal
    entry_atr: Decimal
    stop_loss: Decimal
    take_profit: Decimal


def calculate_long_exit_levels(
    entry_price: Decimal,
    entry_atr: Decimal,
) -> ExitLevels:
    """
    Calcula SL y TP a partir del ATR congelado en la entrada.

    - `SL = entrada - ATR`
    - `TP = entrada + 2 * ATR`
    """
    if entry_price <= 0:
        raise ValueError("entry_price debe ser mayor que 0")

    if entry_atr <= 0:
        raise ValueError("entry_atr debe ser mayor que 0")

    return ExitLevels(
        entry_price=entry_price,
        entry_atr=entry_atr,
        stop_loss=entry_price - entry_atr,
        take_profit=entry_price + 2 * entry_atr,
    )
