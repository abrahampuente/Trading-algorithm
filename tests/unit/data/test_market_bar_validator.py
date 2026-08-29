from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.validation.market_bar_validator import validate_market_bars


def build_bar(
    symbol: str = "AAPL",
    timestamp: datetime | None = None,
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        timestamp=timestamp or datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        timeframe="1d",
        source="test",
        open=Decimal("100.00"),
        high=Decimal("105.00"),
        low=Decimal("99.00"),
        close=Decimal("103.00"),
        volume=Decimal("1000000"),
    )


def test_market_bar_validates_ohlc() -> None:
    bar = build_bar()

    assert bar.symbol == "AAPL"
    assert bar.high == Decimal("105.00")


def test_market_bar_rejects_invalid_ohlc() -> None:
    with pytest.raises(ValidationError):
        MarketBar(
            symbol="AAPL",
            timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
            timeframe="1d",
            source="test",
            open=Decimal("100.00"),
            high=Decimal("105.00"),
            low=Decimal("104.00"),
            close=Decimal("103.00"),
            volume=Decimal("1000000"),
        )


def test_validate_market_bars_rejects_duplicates() -> None:
    bar = build_bar()

    with pytest.raises(ValueError, match="Barra duplicada"):
        validate_market_bars([bar, bar])


def test_validate_market_bars_orders_by_symbol_and_timestamp() -> None:
    later = build_bar(
        symbol="MSFT",
        timestamp=datetime(2024, 1, 3, tzinfo=UTC),
    )
    earlier = build_bar(
        symbol="AAPL",
        timestamp=datetime(2024, 1, 2, tzinfo=UTC),
    )

    result = validate_market_bars([later, earlier])

    assert [bar.symbol for bar in result] == ["AAPL", "MSFT"]
