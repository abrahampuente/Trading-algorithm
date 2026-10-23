from collections.abc import Sequence

from algo_trading.indicators.price_series import PriceBar
from algo_trading.signals.base import Side, Signal
from algo_trading.signals.context import SignalContext
from algo_trading.strategy.long_signal import (
    LongSignal,
    LongSignalGenerator,
    SignalParameters,
)

MOMENTUM_BREAKOUT_V1 = "momentum_breakout_v1"


def signal_from_long_signal(
    long_signal: LongSignal,
    strategy_id: str = MOMENTUM_BREAKOUT_V1,
) -> Signal:
    """Traduce una `LongSignal` de la v1 al contrato multi-factor."""
    return Signal(
        symbol=long_signal.symbol,
        signal_timestamp=long_signal.signal_timestamp,
        side=Side.LONG,
        strategy_id=strategy_id,
        close=long_signal.close,
        atr=long_signal.atr,
        score=long_signal.momentum,
        factors={
            "momentum": long_signal.momentum,
            "upper_band": long_signal.upper_band,
            "previous_close": long_signal.previous_close,
            "previous_upper_band": long_signal.previous_upper_band,
        },
    )


class MomentumBreakoutGenerator:
    """
    Estrategia v1 (momentum + ruptura de Bollinger) como `SignalGenerator`.

    Delega en `LongSignalGenerator` sin modificarlo: emite exactamente las
    mismas señales, con `score = momentum`, de modo que priorizar por `score`
    y ticker equivale a la priorización de la v1. No usa el contexto.
    """

    def __init__(self, parameters: SignalParameters | None = None) -> None:
        self._generator = LongSignalGenerator(parameters)

    @property
    def strategy_id(self) -> str:
        return MOMENTUM_BREAKOUT_V1

    def generate(
        self,
        bars: Sequence[PriceBar],
        context: SignalContext | None = None,
    ) -> tuple[Signal, ...]:
        return tuple(
            signal_from_long_signal(long_signal, self.strategy_id)
            for long_signal in self._generator.generate(bars)
        )
