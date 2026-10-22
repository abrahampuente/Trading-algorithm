import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.data.market_bar import MarketBar
from algo_trading.indicators import calculate_indicators
from algo_trading.strategy import LongSignalGenerator, SignalParameters

START = datetime(2024, 1, 1, tzinfo=UTC)


def build_bars(closes: list[Decimal]) -> list[MarketBar]:
    return [
        MarketBar(
            symbol="AAPL",
            timestamp=START + timedelta(days=index),
            timeframe="1d",
            source="test",
            open=close,
            high=close + 1,
            low=close - 1,
            close=close,
            volume=Decimal("1000"),
        )
        for index, close in enumerate(closes)
    ]


def slow_uptrend(length: int) -> list[Decimal]:
    """Tendencia lineal: el cierre queda siempre dentro de la banda superior."""
    return [Decimal("100") + Decimal("0.1") * index for index in range(length)]


def breakout_closes() -> list[Decimal]:
    """20 barras de tendencia suave y ruptura en el índice 20."""
    return [*slow_uptrend(20), Decimal("110")]


def random_walk_closes(length: int, seed: int) -> list[Decimal]:
    generator = random.Random(seed)
    close = Decimal("100")
    closes: list[Decimal] = []

    for _ in range(length):
        step = Decimal(generator.randint(-150, 250)) / 100
        close = max(Decimal("10"), close + step)
        closes.append(close)

    return closes


def test_emits_signal_at_breakout_close_with_known_values() -> None:
    bars = build_bars(breakout_closes())
    rows = calculate_indicators(bars)

    signals = LongSignalGenerator().generate(bars)

    assert len(signals) == 1
    signal = signals[0]
    assert signal.signal_timestamp == bars[20].timestamp
    assert signal.close == Decimal("110")
    assert signal.previous_close == Decimal("101.9")
    assert signal.upper_band == rows[20].upper_band
    assert signal.previous_upper_band == rows[19].upper_band
    assert signal.atr == rows[20].atr
    assert signal.momentum == Decimal("0.1")
    assert signal.close > signal.upper_band
    assert signal.previous_close <= signal.previous_upper_band


def test_signal_on_last_bar_does_not_need_next_bar() -> None:
    bars = build_bars(breakout_closes())

    signals = LongSignalGenerator().generate(bars)

    assert signals[-1].signal_timestamp == bars[-1].timestamp


def test_no_signal_during_21_bar_warm_up() -> None:
    bars = build_bars([*slow_uptrend(19), Decimal("110")])

    assert LongSignalGenerator().generate(bars) == ()


def test_no_signal_without_band_breakout() -> None:
    bars = build_bars(slow_uptrend(40))

    assert LongSignalGenerator().generate(bars) == ()


def test_no_signal_when_momentum_condition_fails() -> None:
    closes = breakout_closes()
    # Close[t-20] = 105 > Close[t-10] = 101: falla el momentum.
    closes[0] = Decimal("105")
    bars = build_bars(closes)
    rows = calculate_indicators(bars)

    assert rows[20].upper_band is not None
    assert rows[20].close > rows[20].upper_band
    assert LongSignalGenerator().generate(bars) == ()


def test_no_repeated_signal_while_already_above_upper_band() -> None:
    bars = build_bars([*breakout_closes(), Decimal("120")])
    rows = calculate_indicators(bars)

    signals = LongSignalGenerator().generate(bars)

    assert rows[21].upper_band is not None
    assert rows[21].close > rows[21].upper_band
    assert [signal.signal_timestamp for signal in signals] == [bars[20].timestamp]


def test_signals_are_invariant_to_future_bars() -> None:
    bars = build_bars(random_walk_closes(length=250, seed=7))
    generator = LongSignalGenerator()
    full_signals = generator.generate(bars)
    full_rows = calculate_indicators(bars)

    assert len(full_signals) >= 2

    for index in range(len(bars)):
        prefix = bars[: index + 1]
        cutoff = bars[index].timestamp

        assert calculate_indicators(prefix)[-1] == full_rows[index]
        assert generator.generate(prefix) == tuple(
            signal for signal in full_signals if signal.signal_timestamp <= cutoff
        )


def test_changing_future_bars_does_not_change_past_signals() -> None:
    closes = random_walk_closes(length=250, seed=7)
    generator = LongSignalGenerator()
    original = generator.generate(build_bars(closes))

    assert original

    for signal in original:
        cutoff_index = (signal.signal_timestamp - START).days
        # Desplome a partir de t+1: una señal que dependa de t+1 cambiaría.
        altered_closes = [
            *closes[: cutoff_index + 1],
            *(close / 2 for close in closes[cutoff_index + 1 :]),
        ]

        altered = generator.generate(build_bars(altered_closes))

        assert [
            s for s in original if s.signal_timestamp <= signal.signal_timestamp
        ] == [s for s in altered if s.signal_timestamp <= signal.signal_timestamp]


def test_signal_parameters_require_short_lookback_below_long() -> None:
    with pytest.raises(ValueError, match="momentum_short_lookback"):
        SignalParameters(momentum_short_lookback=20, momentum_long_lookback=20)
