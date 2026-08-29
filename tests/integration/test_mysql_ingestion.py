from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from algo_trading.data import IngestionService, MarketBar
from algo_trading.persistence.models import MarketBarRaw

DATABASE_URL = (
    "mysql+pymysql://algo_trading_app:"
    "algo-trading-dev-password@localhost:3306/algo_trading"
)


def build_bar(symbol: str) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
        timeframe="1d",
        source="integration-test",
        open=Decimal("100.00"),
        high=Decimal("105.00"),
        low=Decimal("99.00"),
        close=Decimal("103.00"),
        volume=Decimal("1000000"),
    )


def test_ingest_bars_into_mysql() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with Session(engine) as session:
        session.execute(
            delete(MarketBarRaw).where(MarketBarRaw.source == "integration-test")
        )
        session.commit()

        service = IngestionService(session)
        inserted_count = service.ingest_bars([build_bar("AAPL")])

        persisted_bar = session.scalar(
            select(MarketBarRaw).where(
                MarketBarRaw.symbol == "AAPL",
                MarketBarRaw.source == "integration-test",
            )
        )

        assert inserted_count == 1
        assert persisted_bar is not None
        assert persisted_bar.symbol == "AAPL"
        assert persisted_bar.close == Decimal("103.00000000")

        session.delete(persisted_bar)
        session.commit()

    engine.dispose()
