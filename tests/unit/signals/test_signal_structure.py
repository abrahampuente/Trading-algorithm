import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.data.market_bar import MarketBar
from algo_trading.indicators.price_series import PriceBar
from algo_trading.signals import (
    MOMENTUM_BREAKOUT_V1,
    BenchmarkTrendFilter,
    CompositeSignalGenerator,
    FilterResult,
    MissingReferenceSeriesError,
    MomentumBreakoutGenerator,
    RelativeStrengthFilter,
    Side,
    Signal,
    SignalContext,
    VixRegimeFilter,
)
from algo_trading.strategy import LongSignalGenerator

START = datetime(2024, 1, 1, tzinfo=UTC)


def build_bars(symbol: str, closes: Sequence[Decimal]) -> list[MarketBar]:
    return [
        MarketBar(
            symbol=symbol,
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


def random_walk_closes(length: int, seed: int) -> list[Decimal]:
    generator = random.Random(seed)
    close = Decimal("100")
    closes: list[Decimal] = []

    for _ in range(length):
        step = Decimal(generator.randint(-150, 250)) / 100
        close = max(Decimal("10"), close + step)
        closes.append(close)

    return closes


class AlwaysFilter:
    """Filtro de prueba con resultado fijo."""

    def __init__(self, name: str, passed: bool) -> None:
        self._name = name
        self._passed = passed

    @property
    def name(self) -> str:
        return self._name

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult:
        return FilterResult(
            name=self._name,
            passed=self._passed,
            values={"seen_bars": Decimal(len(bars))},
        )


class PeekingFilter:
    """Intenta leer lo más reciente de cada serie; registra lo que ve."""

    def __init__(self) -> None:
        self.latest_seen: list[tuple[datetime, datetime]] = []

    @property
    def name(self) -> str:
        return "peeking"

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult:
        latest = max(
            [bars[-1].timestamp]
            + [context.bars(symbol)[-1].timestamp for symbol in context.symbols]
        )
        self.latest_seen.append((signal.signal_timestamp, latest))
        return FilterResult(name=self.name, passed=True)


@pytest.mark.parametrize("seed", [1, 7, 42, 2024])
def test_momentum_breakout_matches_v1_long_signals(seed: int) -> None:
    bars = build_bars("AAPL", random_walk_closes(400, seed))

    v1_signals = LongSignalGenerator().generate(bars)
    signals = MomentumBreakoutGenerator().generate(bars)

    assert v1_signals
    assert len(signals) == len(v1_signals)

    for signal, v1_signal in zip(signals, v1_signals, strict=True):
        assert signal.symbol == v1_signal.symbol
        assert signal.signal_timestamp == v1_signal.signal_timestamp
        assert signal.side is Side.LONG
        assert signal.strategy_id == MOMENTUM_BREAKOUT_V1
        assert signal.close == v1_signal.close
        assert signal.atr == v1_signal.atr
        assert signal.score == v1_signal.momentum
        assert signal.factors["momentum"] == v1_signal.momentum


def test_composite_without_filters_emits_base_signals() -> None:
    bars = build_bars("AAPL", random_walk_closes(400, 7))
    base = MomentumBreakoutGenerator()

    result = CompositeSignalGenerator(base).run(bars)

    assert result.accepted == base.generate(bars)
    assert result.filtered == ()


def test_composite_merges_filter_values_and_overrides_strategy_id() -> None:
    bars = build_bars("AAPL", random_walk_closes(400, 7))
    base_signals = MomentumBreakoutGenerator().generate(bars)

    accepted = CompositeSignalGenerator(
        MomentumBreakoutGenerator(),
        filters=[AlwaysFilter("always", passed=True)],
        strategy_id="momentum_filtered",
    ).generate(bars)

    assert len(accepted) == len(base_signals)

    for signal, base_signal in zip(accepted, base_signals, strict=True):
        index = next(
            index
            for index, bar in enumerate(bars)
            if bar.timestamp == base_signal.signal_timestamp
        )
        assert signal.strategy_id == "momentum_filtered"
        assert signal.score == base_signal.score
        assert signal.factors["momentum"] == base_signal.factors["momentum"]
        assert signal.factors["always.seen_bars"] == Decimal(index + 1)


def test_composite_records_every_failing_filter() -> None:
    bars = build_bars("AAPL", random_walk_closes(400, 7))

    result = CompositeSignalGenerator(
        MomentumBreakoutGenerator(),
        filters=[
            AlwaysFilter("first", passed=False),
            AlwaysFilter("second", passed=True),
            AlwaysFilter("third", passed=False),
        ],
    ).run(bars)

    assert result.accepted == ()
    assert result.filtered
    assert all(
        item.failed_filters == ("first", "third") and len(item.results) == 3
        for item in result.filtered
    )


def test_composite_rejects_duplicate_filter_names() -> None:
    with pytest.raises(ValueError, match="nombres distintos"):
        CompositeSignalGenerator(
            MomentumBreakoutGenerator(),
            filters=[AlwaysFilter("same", True), AlwaysFilter("same", False)],
        )


def test_context_until_hides_later_bars() -> None:
    spy = build_bars("SPY", [Decimal(100 + index) for index in range(10)])
    context = SignalContext({"SPY": spy})
    cut = spy[4].timestamp

    view = context.until(cut)

    assert view.bars("SPY") == tuple(spy[:5])
    assert view.close_at("SPY", cut) == Decimal("104")
    assert view.close_at("SPY", spy[5].timestamp) is None
    assert context.close_at("SPY", spy[5].timestamp) == Decimal("105")
    assert context.until(START - timedelta(days=1)).bars("SPY") == ()


def test_context_rejects_unknown_and_mislabelled_series() -> None:
    context = SignalContext({"SPY": build_bars("SPY", [Decimal("100")])})

    with pytest.raises(MissingReferenceSeriesError):
        context.bars("QQQ")

    with pytest.raises(ValueError, match="contiene barras de SPY"):
        SignalContext({"QQQ": build_bars("SPY", [Decimal("100")])})


def test_filters_never_see_data_after_signal() -> None:
    length = 400
    bars = build_bars("AAPL", random_walk_closes(length, 42))
    context = SignalContext(
        {
            "SPY": build_bars("SPY", random_walk_closes(length, 3)),
            "QQQ": build_bars("QQQ", random_walk_closes(length, 4)),
        }
    )
    peeking = PeekingFilter()

    accepted = CompositeSignalGenerator(
        MomentumBreakoutGenerator(), filters=[peeking]
    ).generate(bars, context)

    assert accepted
    assert all(latest <= signal_time for signal_time, latest in peeking.latest_seen)


def test_composite_is_invariant_to_future_bars() -> None:
    length = 500
    cut = 300
    closes = random_walk_closes(length, 2024)
    spy_closes = random_walk_closes(length, 11)
    vix_closes = [Decimal(15 + index % 20) for index in range(length)]

    def run(
        symbol_closes: list[Decimal],
        spy: list[Decimal],
        vix: list[Decimal],
    ) -> tuple[Signal, ...]:
        context = SignalContext(
            {"SPY": build_bars("SPY", spy), "^VIX": build_bars("^VIX", vix)}
        )
        generator = CompositeSignalGenerator(
            MomentumBreakoutGenerator(),
            filters=[
                RelativeStrengthFilter(lookback=20, min_relative_strength=Decimal(-1)),
                BenchmarkTrendFilter(window=50),
                VixRegimeFilter(max_level=Decimal("30")),
            ],
        )
        return generator.generate(build_bars("AAPL", symbol_closes), context)

    def altered(values: list[Decimal]) -> list[Decimal]:
        return values[:cut] + [value * 3 for value in values[cut:]]

    full = run(closes, spy_closes, vix_closes)
    changed = run(altered(closes), altered(spy_closes), altered(vix_closes))
    cut_time = START + timedelta(days=cut - 1)

    before_cut = [signal for signal in full if signal.signal_timestamp <= cut_time]
    assert before_cut
    assert [
        signal for signal in changed if signal.signal_timestamp <= cut_time
    ] == before_cut


def test_lookahead_check_detects_injected_bias() -> None:
    """Un contexto sin `until` deja ver el futuro y rompe la invariancia."""
    length = 500
    cut = 300
    closes = random_walk_closes(length, 2024)
    spy_closes = [Decimal("100")] * length
    cut_time = START + timedelta(days=cut - 1)

    class LeakyFilter:
        name = "leaky"

        def __init__(self, full_context: SignalContext) -> None:
            self._full_context = full_context

        def evaluate(
            self,
            signal: Signal,
            bars: Sequence[PriceBar],
            context: SignalContext,
        ) -> FilterResult:
            last = self._full_context.bars("SPY")[-1].close
            return FilterResult(name=self.name, passed=last < Decimal("500"))

    def run(spy: list[Decimal]) -> tuple[Signal, ...]:
        context = SignalContext({"SPY": build_bars("SPY", spy)})
        return CompositeSignalGenerator(
            MomentumBreakoutGenerator(), filters=[LeakyFilter(context)]
        ).generate(build_bars("AAPL", closes), context)

    full = run(spy_closes)
    changed = run(spy_closes[:cut] + [Decimal("1000")] * (length - cut))

    def prefix(signals: tuple[Signal, ...]) -> list[Signal]:
        return [signal for signal in signals if signal.signal_timestamp <= cut_time]

    assert prefix(full) != prefix(changed)
