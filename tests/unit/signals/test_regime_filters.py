from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.data.market_bar import MarketBar
from algo_trading.signals import (
    BenchmarkTrendFilter,
    RelativeStrengthFilter,
    SectorRotationFilter,
    Side,
    Signal,
    SignalContext,
    VixRegimeFilter,
    VolatilityRegimeFilter,
    percentile_rank,
    relative_strength,
)
from algo_trading.signals.filters import (
    INSUFFICIENT_HISTORY,
    MISSING_REFERENCE_DATA,
    UNMAPPED_SECTOR,
)

START = datetime(2024, 1, 1, tzinfo=UTC)


def build_bars(
    symbol: str,
    closes: Sequence[Decimal],
    ranges: Sequence[Decimal] | None = None,
    offset: int = 0,
) -> list[MarketBar]:
    return [
        MarketBar(
            symbol=symbol,
            timestamp=START + timedelta(days=offset + index),
            timeframe="1d",
            source="test",
            open=close,
            high=close + (ranges[index] if ranges else 1),
            low=close - (ranges[index] if ranges else 1),
            close=close,
            volume=Decimal("1000"),
        )
        for index, close in enumerate(closes)
    ]


def linear(start: int, step: str, length: int) -> list[Decimal]:
    return [Decimal(start) + Decimal(step) * index for index in range(length)]


def signal_at(bars: Sequence[MarketBar]) -> Signal:
    last = bars[-1]
    return Signal(
        symbol=last.symbol,
        signal_timestamp=last.timestamp,
        side=Side.LONG,
        strategy_id="test",
        close=last.close,
        atr=Decimal("1"),
        score=Decimal("0"),
    )


def test_relative_strength_known_value() -> None:
    # Símbolo +20 %, benchmark +10 % -> 1.2 / 1.1 - 1
    value = relative_strength(
        [Decimal("100"), Decimal("110"), Decimal("120")],
        [Decimal("50"), Decimal("52"), Decimal("55")],
        lookback=2,
    )

    assert value == Decimal("1.2") / Decimal("1.1") - 1
    assert relative_strength([Decimal("1")], [Decimal("1")], lookback=1) is None

    with pytest.raises(ValueError, match="alineados"):
        relative_strength([Decimal("1"), Decimal("2")], [Decimal("1")], lookback=1)


def test_percentile_rank_counts_values_at_or_below_last() -> None:
    values = [Decimal(value) for value in (5, 1, 3, 4, 3)]

    assert percentile_rank(values) == Decimal("0.6")
    assert percentile_rank([Decimal("1"), Decimal("9")]) == Decimal("1")
    assert percentile_rank([]) is None


def test_relative_strength_filter_passes_when_outperforming() -> None:
    bars = build_bars("AAPL", linear(100, "1", 30))
    context = SignalContext({"SPY": build_bars("SPY", linear(100, "0.5", 30))})

    result = RelativeStrengthFilter(lookback=20).evaluate(
        signal_at(bars), bars, context
    )

    expected = (Decimal("129") / Decimal("109")) / (
        Decimal("114.5") / Decimal("104.5")
    ) - 1
    assert result.passed
    assert result.name == "relative_strength:SPY"
    assert result.values["relative_strength"] == expected


def test_relative_strength_filter_rejects_underperformance_vs_qqq() -> None:
    bars = build_bars("XOM", linear(100, "0.1", 30))
    context = SignalContext({"QQQ": build_bars("QQQ", linear(100, "2", 30))})

    result = RelativeStrengthFilter(benchmark="QQQ", lookback=20).evaluate(
        signal_at(bars), bars, context
    )

    assert not result.passed
    assert result.values["relative_strength"] < 0


def test_relative_strength_filter_requires_history_and_aligned_benchmark() -> None:
    bars = build_bars("AAPL", linear(100, "1", 30))
    gapped_spy = build_bars("SPY", linear(100, "1", 30))
    del gapped_spy[15]
    rs_filter = RelativeStrengthFilter(lookback=20)

    short = rs_filter.evaluate(
        signal_at(bars[:20]), bars[:20], SignalContext({"SPY": gapped_spy})
    )
    missing = rs_filter.evaluate(
        signal_at(bars), bars, SignalContext({"SPY": gapped_spy})
    )

    assert (short.passed, short.reason) == (False, INSUFFICIENT_HISTORY)
    assert (missing.passed, missing.reason) == (False, MISSING_REFERENCE_DATA)


def sector_context(length: int) -> SignalContext:
    return SignalContext(
        {
            "SPY": build_bars("SPY", linear(100, "1", length)),
            "XLK": build_bars("XLK", linear(100, "3", length)),
            "XLE": build_bars("XLE", linear(100, "2", length)),
            "XLF": build_bars("XLF", linear(100, "0.5", length)),
        }
    )


SECTOR_ETFS = {"technology": "XLK", "energy": "XLE", "financials": "XLF"}
SECTOR_MAP = {"AAPL": "technology", "XOM": "energy", "JPM": "financials"}


@pytest.mark.parametrize(
    ("symbol", "rank", "passed"),
    [("AAPL", 1, True), ("XOM", 2, True), ("JPM", 3, False)],
)
def test_sector_rotation_ranks_sector_etfs_vs_benchmark(
    symbol: str, rank: int, passed: bool
) -> None:
    bars = build_bars(symbol, linear(50, "1", 30))
    sector_filter = SectorRotationFilter(
        sector_map=SECTOR_MAP, sector_etfs=SECTOR_ETFS, lookback=20, top_k=2
    )

    result = sector_filter.evaluate(signal_at(bars), bars, sector_context(30))

    assert result.passed is passed
    assert result.values["sector_rank"] == Decimal(rank)
    assert result.values["sectors_ranked"] == Decimal("3")


def test_sector_rotation_rejects_unmapped_symbol_and_stale_etf() -> None:
    sector_filter = SectorRotationFilter(
        sector_map=SECTOR_MAP, sector_etfs=SECTOR_ETFS, lookback=20, top_k=2
    )
    unmapped_bars = build_bars("PG", linear(50, "1", 30))
    longer_bars = build_bars("AAPL", linear(50, "1", 31))

    unmapped = sector_filter.evaluate(
        signal_at(unmapped_bars), unmapped_bars, sector_context(30)
    )
    stale = sector_filter.evaluate(
        signal_at(longer_bars), longer_bars, sector_context(30)
    )

    assert (unmapped.passed, unmapped.reason) == (False, UNMAPPED_SECTOR)
    assert (stale.passed, stale.reason) == (False, MISSING_REFERENCE_DATA)


def test_sector_rotation_can_let_unmapped_symbols_through() -> None:
    bars = build_bars("GLD", linear(50, "1", 30))
    sector_filter = SectorRotationFilter(
        sector_map=SECTOR_MAP,
        sector_etfs=SECTOR_ETFS,
        lookback=20,
        top_k=1,
        allow_unmapped=True,
    )

    result = sector_filter.evaluate(signal_at(bars), bars, sector_context(30))

    assert (result.passed, result.reason) == (True, UNMAPPED_SECTOR)
    assert result.values == {}


def test_sector_rotation_requires_etf_for_every_sector() -> None:
    with pytest.raises(ValueError, match="utilities"):
        SectorRotationFilter(
            sector_map={"NEE": "utilities"}, sector_etfs={"energy": "XLE"}
        )


def test_volatility_regime_filter_uses_atr_percentile() -> None:
    length = 60
    calm = [Decimal("1")] * (length - 1)
    closes = [Decimal("100")] * length
    spike_bars = build_bars("AAPL", closes, calm + [Decimal("10")])
    calm_bars = build_bars("AAPL", closes, calm + [Decimal("0.5")])
    vol_filter = VolatilityRegimeFilter(
        atr_window=5, lookback=30, max_percentile=Decimal("0.9")
    )
    context = SignalContext()

    spike = vol_filter.evaluate(signal_at(spike_bars), spike_bars, context)
    calm_result = vol_filter.evaluate(signal_at(calm_bars), calm_bars, context)
    short = vol_filter.evaluate(signal_at(calm_bars[:20]), calm_bars[:20], context)

    assert not spike.passed
    assert spike.values["atr_percentile"] == Decimal("1")
    assert calm_result.passed
    assert calm_result.values["atr_percentile"] < Decimal("0.1")
    assert (short.passed, short.reason) == (False, INSUFFICIENT_HISTORY)


def test_vix_regime_filter() -> None:
    bars = build_bars("AAPL", linear(100, "1", 3))
    vix_filter = VixRegimeFilter(max_level=Decimal("25"))

    def evaluate(vix_close: str, offset: int = 0) -> tuple[bool, str | None]:
        vix = build_bars("^VIX", [Decimal(vix_close)] * 3, offset=offset)
        result = vix_filter.evaluate(
            signal_at(bars), bars, SignalContext({"^VIX": vix})
        )
        return result.passed, result.reason

    assert evaluate("18") == (True, None)
    assert evaluate("25") == (True, None)
    assert evaluate("32") == (False, None)
    assert evaluate("18", offset=-1) == (False, MISSING_REFERENCE_DATA)


def test_benchmark_trend_filter() -> None:
    bars = build_bars("AAPL", linear(100, "1", 30))
    trend_filter = BenchmarkTrendFilter(window=20)

    def evaluate(spy: list[Decimal]) -> tuple[bool, str | None]:
        context = SignalContext({"SPY": build_bars("SPY", spy, offset=30 - len(spy))})
        result = trend_filter.evaluate(signal_at(bars), bars, context)
        return result.passed, result.reason

    assert evaluate(linear(100, "1", 30)) == (True, None)
    assert evaluate(linear(200, "-1", 30)) == (False, None)
    assert evaluate(linear(100, "1", 10)) == (False, INSUFFICIENT_HISTORY)
    assert evaluate([]) == (False, MISSING_REFERENCE_DATA)
