from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from algo_trading.persistence.models import Base, MarketBarRaw
from algo_trading.persistence.repositories import RawMarketBarQueryRepository


def build_bar(timestamp: datetime, symbol: str = "AAPL") -> MarketBarRaw:
    return MarketBarRaw(
        symbol=symbol,
        timestamp=timestamp,
        timeframe="1d",
        source="test",
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("103"),
        volume=Decimal("1000000"),
    )


def test_get_by_symbol_returns_ordered_bars_in_half_open_interval() -> None:
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

        repository = RawMarketBarQueryRepository(session)

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


def test_get_by_symbol_filters_by_source() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        first = build_bar(datetime(2024, 1, 1))
        second = MarketBarRaw(
            symbol="AAPL",
            timestamp=datetime(2024, 1, 2),
            timeframe="1d",
            source="other-source",
            open=Decimal("100"),
            high=Decimal("105"),
            low=Decimal("99"),
            close=Decimal("103"),
            volume=Decimal("1000000"),
        )

        second.source = "other-source"

        session.add_all([first, second])
        session.commit()

        repository = RawMarketBarQueryRepository(session)

        bars = repository.get_by_symbol(
            symbol="AAPL",
            start=datetime(2024, 1, 1),
            end=datetime(2024, 1, 3),
            source="test",
        )

        assert len(bars) == 1
        assert bars[0].source == "test"

    engine.dispose()
