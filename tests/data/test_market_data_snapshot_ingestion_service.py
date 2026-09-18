from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.market_data_snapshot_ingestion_service import (
    InconsistentSnapshotRequestError,
    MarketDataSnapshotIngestionService,
)
from algo_trading.data.providers.dto import (
    CorporateActionRequest,
    DataRequest,
)
from algo_trading.data.providers.in_memory import InMemoryMarketDataProvider
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)
from algo_trading.persistence.models.corporate_actions import CorporateAction
from algo_trading.persistence.models.data_snapshot import DataSnapshotModel
from algo_trading.persistence.models.market_data import MarketBarRaw

DATABASE_URL = (
    "mysql+pymysql://algo_trading_app:"
    "algo-trading-dev-password@localhost:3306/algo_trading"
)

SOURCE = "provider-ingestion-integration-test"
SNAPSHOT_ID = (
    "provider-ingestion-integration-test:1d:"
    "2024-01-01T00:00:00+00:00:"
    "2024-01-31T23:59:59+00:00"
)


def build_provider() -> InMemoryMarketDataProvider:
    return InMemoryMarketDataProvider(
        bars=(
            MarketBar(
                symbol="AAPL",
                timestamp=datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
                timeframe="1d",
                source=SOURCE,
                open=Decimal("100"),
                high=Decimal("105"),
                low=Decimal("99"),
                close=Decimal("103"),
                volume=Decimal("1000000"),
            ),
        ),
        splits=(
            CorporateSplit(
                symbol="AAPL",
                ex_date=date(2024, 1, 15),
                ratio=Decimal("2"),
                source=SOURCE,
            ),
        ),
        dividends=(
            CorporateDividend(
                symbol="AAPL",
                ex_date=date(2024, 1, 20),
                amount=Decimal("0.25"),
                source=SOURCE,
            ),
        ),
    )


def build_data_request() -> DataRequest:
    return DataRequest(
        symbols=("AAPL",),
        start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        end=datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC),
        timeframe="1d",
        source=SOURCE,
    )


def build_corporate_action_request() -> CorporateActionRequest:
    return CorporateActionRequest(
        symbols=("AAPL",),
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        source=SOURCE,
    )


def clean_test_data(session: Session) -> None:
    session.execute(delete(MarketBarRaw).where(MarketBarRaw.source == SOURCE))
    session.execute(delete(CorporateAction).where(CorporateAction.source == SOURCE))
    session.execute(
        delete(DataSnapshotModel).where(DataSnapshotModel.snapshot_id == SNAPSHOT_ID)
    )
    session.commit()


def test_ingests_provider_snapshot_into_mysql() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with Session(engine) as session:
        clean_test_data(session)

        service = MarketDataSnapshotIngestionService(
            provider=build_provider(),
            session=session,
        )

        metadata = service.ingest(
            data_request=build_data_request(),
            corporate_action_request=build_corporate_action_request(),
        )

        persisted_snapshot = session.scalar(
            select(DataSnapshotModel).where(
                DataSnapshotModel.snapshot_id == SNAPSHOT_ID
            )
        )
        persisted_bar = session.scalar(
            select(MarketBarRaw).where(MarketBarRaw.source == SOURCE)
        )
        persisted_actions = session.scalars(
            select(CorporateAction).where(CorporateAction.source == SOURCE)
        ).all()

        assert metadata.snapshot_id == SNAPSHOT_ID

        assert persisted_snapshot is not None
        assert persisted_snapshot.bars_count == 1
        assert persisted_snapshot.splits_count == 1
        assert persisted_snapshot.dividends_count == 1

        assert persisted_bar is not None
        assert persisted_bar.symbol == "AAPL"
        assert len(persisted_actions) == 2

        clean_test_data(session)

    engine.dispose()


def test_rejects_requests_with_different_symbol_sets() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with Session(engine) as session:
        service = MarketDataSnapshotIngestionService(
            provider=build_provider(),
            session=session,
        )

        inconsistent_request = CorporateActionRequest(
            symbols=("MSFT",),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            source=SOURCE,
        )

        with pytest.raises(InconsistentSnapshotRequestError):
            service.ingest(
                data_request=build_data_request(),
                corporate_action_request=inconsistent_request,
            )

    engine.dispose()
