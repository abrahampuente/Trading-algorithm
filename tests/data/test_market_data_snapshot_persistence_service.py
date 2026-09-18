from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.market_data_snapshot_persistence_service import (
    MarketDataSnapshotPersistenceService,
)
from algo_trading.data.providers.dto import DataSnapshot, MarketDataSnapshot
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)
from algo_trading.persistence.models.corporate_actions import CorporateAction
from algo_trading.persistence.models.data_snapshot import DataSnapshotModel
from algo_trading.persistence.models.market_data import MarketBarRaw
from algo_trading.persistence.repositories.data_snapshot_repository import (
    DuplicateDataSnapshotError,
)

DATABASE_URL = (
    "mysql+pymysql://algo_trading_app:"
    "algo-trading-dev-password@localhost:3306/algo_trading"
)

SOURCE = "snapshot-integration-test"
SNAPSHOT_ID = "snapshot-integration-test:1d:2024-01-01:2024-01-31"


def build_snapshot() -> MarketDataSnapshot:
    return MarketDataSnapshot(
        metadata=DataSnapshot(
            snapshot_id=SNAPSHOT_ID,
            source=SOURCE,
            retrieved_at=datetime(2024, 1, 31, 22, 0, tzinfo=UTC),
            timeframe="1d",
            bars_count=1,
            splits_count=1,
            dividends_count=1,
            checksum="snapshot-integration-checksum",
        ),
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


def clean_test_data(session: Session) -> None:
    session.execute(delete(MarketBarRaw).where(MarketBarRaw.source == SOURCE))
    session.execute(delete(CorporateAction).where(CorporateAction.source == SOURCE))
    session.execute(
        delete(DataSnapshotModel).where(DataSnapshotModel.snapshot_id == SNAPSHOT_ID)
    )
    session.commit()


def test_persists_complete_market_data_snapshot_atomically() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with Session(engine) as session:
        clean_test_data(session)

        service = MarketDataSnapshotPersistenceService(session)
        service.persist(build_snapshot())

        persisted_snapshot = session.scalar(
            select(DataSnapshotModel).where(
                DataSnapshotModel.snapshot_id == SNAPSHOT_ID
            )
        )
        persisted_bar = session.scalar(
            select(MarketBarRaw).where(
                MarketBarRaw.symbol == "AAPL",
                MarketBarRaw.source == SOURCE,
            )
        )
        persisted_actions = session.scalars(
            select(CorporateAction).where(CorporateAction.source == SOURCE)
        ).all()

        assert persisted_snapshot is not None
        assert persisted_snapshot.bars_count == 1
        assert persisted_snapshot.splits_count == 1
        assert persisted_snapshot.dividends_count == 1

        assert persisted_bar is not None
        assert persisted_bar.close == Decimal("103.00000000")

        assert len(persisted_actions) == 2

        clean_test_data(session)

    engine.dispose()


def test_rolls_back_all_data_when_snapshot_already_exists() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with Session(engine) as session:
        clean_test_data(session)

        service = MarketDataSnapshotPersistenceService(session)
        service.persist(build_snapshot())

        with pytest.raises(DuplicateDataSnapshotError):
            service.persist(build_snapshot())

        persisted_bars = session.scalars(
            select(MarketBarRaw).where(MarketBarRaw.source == SOURCE)
        ).all()
        persisted_actions = session.scalars(
            select(CorporateAction).where(CorporateAction.source == SOURCE)
        ).all()

        assert len(persisted_bars) == 1
        assert len(persisted_actions) == 2

        clean_test_data(session)

    engine.dispose()
