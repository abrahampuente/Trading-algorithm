from decimal import Decimal

from pydantic import BaseModel, ConfigDict

DEFAULT_STOP_LOSS_ATR_MULTIPLE = Decimal("1")
DEFAULT_TAKE_PROFIT_ATR_MULTIPLE = Decimal("2")


class ExitLevels(BaseModel):
    """Niveles fijos de salida de una posición Long; no se recalculan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_price: Decimal
    entry_atr: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    stop_loss_atr_multiple: Decimal = DEFAULT_STOP_LOSS_ATR_MULTIPLE
    take_profit_atr_multiple: Decimal = DEFAULT_TAKE_PROFIT_ATR_MULTIPLE


def calculate_long_exit_levels(
    entry_price: Decimal,
    entry_atr: Decimal,
    stop_loss_atr_multiple: Decimal = DEFAULT_STOP_LOSS_ATR_MULTIPLE,
    take_profit_atr_multiple: Decimal = DEFAULT_TAKE_PROFIT_ATR_MULTIPLE,
) -> ExitLevels:
    """
    Calcula SL y TP a partir del ATR congelado en la entrada.

    - `SL = entrada - stop_loss_atr_multiple * ATR` (v1: 1)
    - `TP = entrada + take_profit_atr_multiple * ATR` (v1: 2)
    """
    if entry_price <= 0:
        raise ValueError("entry_price debe ser mayor que 0")

    if entry_atr <= 0:
        raise ValueError("entry_atr debe ser mayor que 0")

    if stop_loss_atr_multiple <= 0:
        raise ValueError("stop_loss_atr_multiple debe ser mayor que 0")

    if take_profit_atr_multiple <= 0:
        raise ValueError("take_profit_atr_multiple debe ser mayor que 0")

    return ExitLevels(
        entry_price=entry_price,
        entry_atr=entry_atr,
        stop_loss=entry_price - stop_loss_atr_multiple * entry_atr,
        take_profit=entry_price + take_profit_atr_multiple * entry_atr,
        stop_loss_atr_multiple=stop_loss_atr_multiple,
        take_profit_atr_multiple=take_profit_atr_multiple,
    )
