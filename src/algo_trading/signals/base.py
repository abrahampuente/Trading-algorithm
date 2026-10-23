from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.indicators.price_series import PriceBar
from algo_trading.signals.context import SignalContext


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Signal(BaseModel):
    """
    Señal multi-factor emitida al cierre de `signal_timestamp`.

    Como `LongSignal`, no contiene precio de entrada: la entrada se simula en
    la apertura de la siguiente sesión. `score` prioriza señales simultáneas
    (mayor primero) y `factors` guarda, para auditoría, el valor en `t` de
    cada factor que intervino en la decisión.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    signal_timestamp: datetime
    side: Side
    strategy_id: str = Field(min_length=1, max_length=64)
    close: Decimal = Field(gt=0)
    atr: Decimal = Field(gt=0)
    score: Decimal
    factors: dict[str, Decimal] = Field(default_factory=dict)


class SignalGenerator(Protocol):
    """
    Generador de señales de un símbolo.

    `generate` evalúa cada barra de `bars` (un único símbolo, timestamps
    estrictamente crecientes) usando sólo barras y series de referencia con
    timestamp menor o igual que el de esa barra.
    """

    @property
    def strategy_id(self) -> str: ...

    def generate(
        self,
        bars: Sequence[PriceBar],
        context: SignalContext | None = None,
    ) -> tuple[Signal, ...]: ...
