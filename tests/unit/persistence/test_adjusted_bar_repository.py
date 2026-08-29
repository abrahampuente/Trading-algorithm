from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from algo_trading.persistence.models import Base, MarketBarAdjusted
from algo_trading.persistence.repositories import AdjustedMarketBarRepository


def build_adjusted_bar() -> MarketBarAdjusted:
    return MarketBarAdjusted(
        symbol="AAPL",
        timestamp=datetime(2020, 1, 1, 14, 30),
        timeframe="1d",
        source="test",
        open=Decimal("50"),
        high=Decimal("52.5"),
        low=Decimal("49.5"),
        close=Decimal("51.5"),
        volume=Decimal("2000000"),
        adjustment_factor=Decimal("0.5"),
        adjustment_version="split-v1",
    )


def test_add_adjusted_bar() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        repository = AdjustedMarketBarRepository(session)

        entity = repository.add(build_adjusted_bar())
        repository.flush()
        session.commit()

        persisted_bar = session.scalar(
            select(MarketBarAdjusted).where(
                MarketBarAdjusted.symbol == "AAPL",
            )
        )

        assert entity is persisted_bar
        assert persisted_bar is not None
        assert persisted_bar.adjustment_factor == Decimal("0.500000000000")

    engine.dispose()
