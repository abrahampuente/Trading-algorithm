from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from algo_trading.data import IngestionService, MarketBar
from algo_trading.persistence.models import Base, MarketBarRaw


def build_bar(symbol: str, day: int) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2024, 1, day, 14, 30, tzinfo=UTC),
        timeframe="1d",
        source="test",
        open=Decimal("100.00"),
        high=Decimal("105.00"),
        low=Decimal("99.00"),
        close=Decimal("103.00"),
        volume=Decimal("1000000"),
    )


def test_ingest_bars_persists_complete_collection() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        service = IngestionService(session)

        inserted_count = service.ingest_bars(
            [
                build_bar("AAPL", 2),
                build_bar("MSFT", 2),
            ]
        )

        persisted_bars = session.scalars(
            select(MarketBarRaw).order_by(MarketBarRaw.symbol)
        ).all()

        assert inserted_count == 2
        assert [bar.symbol for bar in persisted_bars] == ["AAPL", "MSFT"]

    engine.dispose()


def test_ingest_bars_rolls_back_when_repository_fails() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        service = IngestionService(session)

        bars = [
            build_bar("AAPL", 2),
            build_bar("AAPL", 2),
        ]

        try:
            service.ingest_bars(bars)
        except ValueError:
            pass

        persisted_bars = session.scalars(select(MarketBarRaw)).all()

        assert persisted_bars == []

    engine.dispose()
