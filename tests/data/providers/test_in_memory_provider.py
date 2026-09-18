from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.providers.dto import (
    CorporateActionRequest,
    DataRequest,
)
from algo_trading.data.providers.in_memory import InMemoryMarketDataProvider
from algo_trading.domain.corporate_actions.dto import CorporateDividend


@pytest.fixture
def provider() -> InMemoryMarketDataProvider:
    return InMemoryMarketDataProvider(
        bars=(
            MarketBar(
                symbol="AAA",
                timestamp=datetime(2025, 1, 2, tzinfo=UTC),
                timeframe="1d",
                source="fake",
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=Decimal("1000"),
            ),
        ),
        dividends=(
            CorporateDividend(
                symbol="AAA",
                ex_date=date(2025, 1, 3),
                amount=Decimal("0.10"),
                source="fake",
            ),
        ),
    )


def test_filters_bars_by_request(provider: InMemoryMarketDataProvider) -> None:
    request = DataRequest(
        symbols=("AAA",),
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 3, tzinfo=UTC),
        timeframe="1d",
        source="fake",
    )

    result = provider.get_bars(request)

    assert len(result) == 1
    assert result[0].symbol == "AAA"


def test_builds_snapshot_with_consistent_counts(
    provider: InMemoryMarketDataProvider,
) -> None:
    data_request = DataRequest(
        symbols=("AAA",),
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 1, 3, tzinfo=UTC),
        timeframe="1d",
        source="fake",
    )
    action_request = CorporateActionRequest(
        symbols=("AAA",),
        start=date(2025, 1, 1),
        end=date(2025, 1, 3),
        source="fake",
    )

    snapshot = provider.get_snapshot(data_request, action_request)

    assert snapshot.metadata.bars_count == 1
    assert snapshot.metadata.dividends_count == 1


def test_rejects_invalid_date_range() -> None:
    with pytest.raises(ValidationError):
        DataRequest(
            symbols=("AAA",),
            start=datetime(2025, 1, 3, tzinfo=UTC),
            end=datetime(2025, 1, 1, tzinfo=UTC),
            timeframe="1d",
            source="fake",
        )
