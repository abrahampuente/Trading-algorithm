from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from algo_trading.portfolio.exit_levels import ExitLevels


class ExitReason(StrEnum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TIME_EXIT = "TIME_EXIT"
    END_OF_DATA = "END_OF_DATA"


class ExitDecision(BaseModel):
    """Salida decidida en una barra, con su precio teórico (antes de slippage)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: ExitReason
    theoretical_price: Decimal


def evaluate_bar_exit(
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    levels: ExitLevels,
) -> ExitDecision | None:
    """
    Evalúa SL y TP de una posición Long en una barra.

    Política conservadora:

    - Si la barra alcanza el SL (`low <= SL`), sale por STOP_LOSS aunque
      también alcance el TP: el SL tiene prioridad absoluta.
    - Si la apertura ya está por debajo del SL, se ejecuta a la apertura,
      que es peor que el nivel solicitado.
    - Si sólo alcanza el TP (`high >= TP`), se ejecuta al nivel del TP,
      incluso con gap alcista: no se asume mejora de precio.
    """
    if low <= levels.stop_loss:
        return ExitDecision(
            reason=ExitReason.STOP_LOSS,
            theoretical_price=min(open_price, levels.stop_loss),
        )

    if high >= levels.take_profit:
        return ExitDecision(
            reason=ExitReason.TAKE_PROFIT,
            theoretical_price=levels.take_profit,
        )

    return None


def evaluate_time_exit(open_price: Decimal, levels: ExitLevels) -> ExitDecision:
    """
    Salida temporal a la apertura.

    Si la apertura ya está por debajo del SL, la orden stop se habría
    ejecutado igualmente a esa apertura y se registra como STOP_LOSS.
    """
    if open_price <= levels.stop_loss:
        return ExitDecision(
            reason=ExitReason.STOP_LOSS,
            theoretical_price=open_price,
        )

    return ExitDecision(reason=ExitReason.TIME_EXIT, theoretical_price=open_price)
