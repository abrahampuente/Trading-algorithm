from datetime import date, datetime
from decimal import Decimal

from algo_trading.data.adjustments import AdjustedMarketBarBuilder
from algo_trading.persistence.models.market_data import MarketBarRaw


def build_raw_bar() -> MarketBarRaw:
    return MarketBarRaw(
        symbol="AAPL",
        timestamp=datetime(2020, 1, 1, 14, 30),
        timeframe="1d",
        source="test",
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("103"),
        volume=Decimal("1000000"),
    )


def test_build_adjusted_bar_with_split() -> None:
    builder = AdjustedMarketBarBuilder("split-v1")

    adjusted_bar = builder.build(
        raw_bar=build_raw_bar(),
        splits=[
            (date(2021, 1, 2), Decimal("2")),
        ],
    )

    assert adjusted_bar.symbol == "AAPL"
    assert adjusted_bar.open == Decimal("50")
    assert adjusted_bar.high == Decimal("52.5")
    assert adjusted_bar.low == Decimal("49.5")
    assert adjusted_bar.close == Decimal("51.5")
    assert adjusted_bar.volume == Decimal("2000000")
    assert adjusted_bar.adjustment_factor == Decimal("0.5")
    assert adjusted_bar.adjustment_version == "split-v1"


def test_build_bar_without_applicable_split() -> None:
    builder = AdjustedMarketBarBuilder("split-v1")

    adjusted_bar = builder.build(
        raw_bar=build_raw_bar(),
        splits=[
            (date(2019, 1, 2), Decimal("2")),
        ],
    )

    assert adjusted_bar.adjustment_factor == Decimal("1")
    assert adjusted_bar.open == Decimal("100")
    assert adjusted_bar.volume == Decimal("1000000")
