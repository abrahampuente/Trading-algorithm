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


V1_BAND_FACTORS = ("upper_band", "previous_close", "previous_upper_band")


def long_signal_from_signal(signal: Signal) -> LongSignal:
    """
    Traduce una `Signal` Long con los factores de banda de la v1 a
    `LongSignal`, el contrato que usa hoy el motor de backtest.

    `momentum` toma el `score` de la señal, que es el criterio de prioridad
    del allocator. Falla con señales Short o sin los factores de la v1.
    """
    if signal.side is not Side.LONG:
        raise ValueError(f"El motor v1 sólo admite señales LONG: {signal.side}")

    missing = [name for name in V1_BAND_FACTORS if name not in signal.factors]

    if missing:
        raise ValueError(
            f"La señal de {signal.symbol} no tiene los factores v1: "
            + ", ".join(missing)
        )

    return LongSignal(
        symbol=signal.symbol,
        signal_timestamp=signal.signal_timestamp,
        close=signal.close,
        upper_band=signal.factors["upper_band"],
        previous_close=signal.factors["previous_close"],
        previous_upper_band=signal.factors["previous_upper_band"],
        atr=signal.atr,
        momentum=signal.score,
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
