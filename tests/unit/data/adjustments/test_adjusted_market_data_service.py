from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from algo_trading.data.adjustments import AdjustedMarketDataService
from algo_trading.persistence.models import Base, MarketBarAdjusted, MarketBarRaw


def build_raw_bar(symbol: str = "AAPL") -> MarketBarRaw:
    return MarketBarRaw(
        symbol=symbol,
        timestamp=datetime(2020, 1, 1, 14, 30),
        timeframe="1d",
        source="test",
        open=Decimal("100"),
        high=Decimal("105"),
        low=Decimal("99"),
        close=Decimal("103"),
        volume=Decimal("1000000"),
    )


def test_service_builds_and_persists_adjusted_bars() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        service = AdjustedMarketDataService(
            session=session,
            adjustment_version="split-v1",
        )

        inserted_count = service.build_and_persist(
            raw_bars=[build_raw_bar()],
            splits_by_symbol={
                "AAPL": [
                    (date(2021, 1, 2), Decimal("2")),
                ],
            },
        )

        adjusted_bar = session.scalar(
            select(MarketBarAdjusted).where(
                MarketBarAdjusted.symbol == "AAPL",
            )
        )

        assert inserted_count == 1
        assert adjusted_bar is not None
        assert adjusted_bar.close == Decimal("51.5")
        assert adjusted_bar.volume == Decimal("2000000")
        assert adjusted_bar.adjustment_factor == Decimal("0.5")
        assert adjusted_bar.adjustment_version == "split-v1"

    engine.dispose()


def test_service_uses_factor_one_without_splits() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        service = AdjustedMarketDataService(
            session=session,
            adjustment_version="split-v1",
        )

        inserted_count = service.build_and_persist(
            raw_bars=[build_raw_bar("MSFT")],
            splits_by_symbol={},
        )

        adjusted_bar = session.scalar(
            select(MarketBarAdjusted).where(
                MarketBarAdjusted.symbol == "MSFT",
            )
        )

        assert inserted_count == 1
        assert adjusted_bar is not None
        assert adjusted_bar.adjustment_factor == Decimal("1")
        assert adjusted_bar.close == Decimal("103")

    engine.dispose()
