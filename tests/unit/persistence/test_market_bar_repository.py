from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from algo_trading.data.market_bar import MarketBar
from algo_trading.persistence.models import Base
from algo_trading.persistence.repositories import RawMarketBarRepository


def build_bar() -> MarketBar:
    return MarketBar(
        symbol="AAPL",
        timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        timeframe="1d",
        source="test",
        open=Decimal("100.00"),
        high=Decimal("105.00"),
        low=Decimal("99.00"),
        close=Decimal("103.00"),
        volume=Decimal("1000000"),
    )


def test_add_converts_market_bar_to_database_entity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repository = RawMarketBarRepository(session)

        entity = repository.add(build_bar())

        assert entity.symbol == "AAPL"
        assert entity.timeframe == "1d"
        assert entity.source == "test"
        assert entity.open == Decimal("100.00")
        assert entity.timestamp == datetime(2024, 1, 2, 14, 30)

    engine.dispose()


def test_add_many_adds_all_entities() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    bars = [
        build_bar(),
        build_bar().model_copy(
            update={
                "symbol": "MSFT",
            }
        ),
    ]

    with Session(engine) as session:
        repository = RawMarketBarRepository(session)

        entities = repository.add_many(bars)

        assert len(entities) == 2
        assert [entity.symbol for entity in entities] == ["AAPL", "MSFT"]

    engine.dispose()
