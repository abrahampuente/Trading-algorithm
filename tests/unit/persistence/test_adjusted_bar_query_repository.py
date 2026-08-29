from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from algo_trading.persistence.models import Base, MarketBarAdjusted
from algo_trading.persistence.repositories import (
    AdjustedMarketBarQueryRepository,
)


def build_bar(
    timestamp: datetime,
    symbol: str = "AAPL",
    source: str = "test",
    version: str = "split-v1",
) -> MarketBarAdjusted:
    return MarketBarAdjusted(
        symbol=symbol,
        timestamp=timestamp,
        timeframe="1d",
        source=source,
        open=Decimal("50"),
        high=Decimal("52.5"),
        low=Decimal("49.5"),
        close=Decimal("51.5"),
        volume=Decimal("2000000"),
        adjustment_factor=Decimal("0.5"),
        adjustment_version=version,
    )


def test_get_by_symbol_returns_ordered_adjusted_bars() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                build_bar(datetime(2024, 1, 3)),
                build_bar(datetime(2024, 1, 1)),
                build_bar(datetime(2024, 1, 2)),
            ]
        )
        session.commit()

        repository = AdjustedMarketBarQueryRepository(session)

        bars = repository.get_by_symbol(
            symbol="AAPL",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 1, 3),
        )

        assert [bar.timestamp for bar in bars] == [
            datetime(2024, 1, 1),
            datetime(2024, 1, 2),
        ]

    engine.dispose()


def test_get_by_symbol_filters_by_adjustment_version() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                build_bar(
                    datetime(2024, 1, 1),
                    version="split-v1",
                ),
                build_bar(
                    datetime(2024, 1, 2),
                    version="split-v2",
                ),
            ]
        )
        session.commit()

        repository = AdjustedMarketBarQueryRepository(session)

        bars = repository.get_by_symbol(
            symbol="AAPL",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 1, 3),
            adjustment_version="split-v2",
        )

        assert len(bars) == 1
        assert bars[0].adjustment_version == "split-v2"

    engine.dispose()


def test_get_by_symbol_filters_by_source() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                build_bar(datetime(2024, 1, 1), source="provider-a"),
                build_bar(datetime(2024, 1, 2), source="provider-b"),
            ]
        )
        session.commit()

        repository = AdjustedMarketBarQueryRepository(session)

        bars = repository.get_by_symbol(
            symbol="AAPL",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 1, 3),
            source="provider-a",
        )

        assert len(bars) == 1
        assert bars[0].source == "provider-a"

    engine.dispose()
