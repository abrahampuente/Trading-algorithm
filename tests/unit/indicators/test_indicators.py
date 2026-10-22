from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.data.market_bar import MarketBar
from algo_trading.indicators import (
    IndicatorParameters,
    bollinger_bands,
    calculate_indicators,
    rolling_population_std,
    simple_moving_average,
    true_ranges,
    wilder_atr,
)
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted

START = datetime(2024, 1, 1, tzinfo=UTC)


def build_bar(
    index: int,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    symbol: str = "AAPL",
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        timestamp=START + timedelta(days=index),
        timeframe="1d",
        source="test",
        open=close,
        high=high,
        low=low,
        close=close,
        volume=Decimal("1000"),
    )


def build_bars_from_closes(closes: list[Decimal]) -> list[MarketBar]:
    return [
        build_bar(
            index=index,
            high=close + 1,
            low=close - 1,
            close=close,
        )
        for index, close in enumerate(closes)
    ]


def test_simple_moving_average_uses_trailing_window() -> None:
    values = [Decimal(value) for value in range(1, 26)]

    sma = simple_moving_average(values, window=20)

    assert sma[:19] == (None,) * 19
    assert sma[19] == Decimal("10.5")
    assert sma[20] == Decimal("11.5")
    assert sma[24] == Decimal("15.5")


def test_simple_moving_average_rejects_invalid_window() -> None:
    with pytest.raises(ValueError, match="window"):
        simple_moving_average([Decimal("1")], window=0)


def test_rolling_std_is_population_std_with_ddof_zero() -> None:
    # Ejemplo clásico: media 5, desviación poblacional 2 (muestral ≈ 2.138).
    values = [Decimal(value) for value in (2, 4, 4, 4, 5, 5, 7, 9)]

    deviations = rolling_population_std(values, window=8)

    assert deviations[:7] == (None,) * 7
    assert deviations[7] == Decimal("2")


def test_bollinger_bands_are_sma_plus_minus_multiplier_std() -> None:
    values = [Decimal(value) for value in (2, 4, 4, 4, 5, 5, 7, 9)]

    bands = bollinger_bands(values, window=8, std_multiplier=Decimal("2"))

    band = bands[7]
    assert band is not None
    assert band.middle == Decimal("5")
    assert band.standard_deviation == Decimal("2")
    assert band.upper == Decimal("9")
    assert band.lower == Decimal("1")


def test_bollinger_bands_collapse_on_constant_series() -> None:
    values = [Decimal("100")] * 20

    band = bollinger_bands(values, window=20, std_multiplier=Decimal("2"))[19]

    assert band is not None
    assert band.upper == band.middle == band.lower == Decimal("100")


def test_true_range_accounts_for_gaps_against_previous_close() -> None:
    bars = [
        build_bar(0, high=Decimal("10"), low=Decimal("8"), close=Decimal("9")),
        build_bar(1, high=Decimal("12"), low=Decimal("9"), close=Decimal("11")),
        # Gap bajista: |Low - Close[t-1]| = |7 - 11| = 4.
        build_bar(2, high=Decimal("9"), low=Decimal("7"), close=Decimal("8")),
        # Gap alcista: |High - Close[t-1]| = |15 - 8| = 7.
        build_bar(3, high=Decimal("15"), low=Decimal("13"), close=Decimal("14")),
    ]

    assert true_ranges(bars) == (
        None,
        Decimal("3"),
        Decimal("4"),
        Decimal("7"),
    )


def test_wilder_atr_seeds_with_mean_and_then_smooths() -> None:
    bars = [
        build_bar(0, high=Decimal("10"), low=Decimal("8"), close=Decimal("9")),
        build_bar(1, high=Decimal("12"), low=Decimal("9"), close=Decimal("11")),
        build_bar(2, high=Decimal("9"), low=Decimal("7"), close=Decimal("8")),
        build_bar(3, high=Decimal("15"), low=Decimal("13"), close=Decimal("14")),
        build_bar(4, high=Decimal("15"), low=Decimal("13"), close=Decimal("14")),
    ]

    atr = wilder_atr(bars, window=3)

    seed = Decimal("14") / 3
    assert atr[:3] == (None, None, None)
    assert atr[3] == seed
    assert atr[4] == (seed * 2 + Decimal("2")) / 3


def test_wilder_atr_20_requires_21_bars() -> None:
    bars = build_bars_from_closes([Decimal("100")] * 21)

    atr = wilder_atr(bars, window=20)

    assert atr[19] is None
    assert atr[20] == Decimal("2")


def test_calculate_indicators_uses_v1_defaults_and_warm_up() -> None:
    closes = [Decimal("100") + Decimal("0.5") * index for index in range(21)]
    bars = build_bars_from_closes(closes)

    rows = calculate_indicators(bars)

    assert IndicatorParameters().bollinger_window == 20
    assert IndicatorParameters().atr_window == 20
    assert rows[18].sma is None
    assert rows[18].upper_band is None
    assert rows[19].sma == sum(closes[:20], Decimal("0")) / 20
    assert rows[19].atr is None
    assert rows[20].atr is not None
    assert [row.timestamp for row in rows] == [bar.timestamp for bar in bars]


def test_calculate_indicators_accepts_adjusted_orm_bars() -> None:
    bars = [
        MarketBarAdjusted(
            symbol="AAPL",
            timestamp=datetime(2024, 1, 1) + timedelta(days=index),
            timeframe="1d",
            source="test",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1000"),
            adjustment_factor=Decimal("1"),
            adjustment_version="v1",
        )
        for index in range(21)
    ]

    rows = calculate_indicators(bars)

    assert rows[20].sma == Decimal("100")
    assert rows[20].atr == Decimal("2")


def test_calculate_indicators_rejects_unsorted_bars() -> None:
    bars = build_bars_from_closes([Decimal("100"), Decimal("101")])

    with pytest.raises(ValueError, match="ordenada"):
        calculate_indicators(list(reversed(bars)))


def test_calculate_indicators_rejects_mixed_symbols() -> None:
    bars = [
        build_bar(0, Decimal("101"), Decimal("99"), Decimal("100"), symbol="AAA"),
        build_bar(1, Decimal("101"), Decimal("99"), Decimal("100"), symbol="BBB"),
    ]

    with pytest.raises(ValueError, match="único símbolo"):
        calculate_indicators(bars)
