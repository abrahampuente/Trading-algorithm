from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from algo_trading.data.providers.dto import DataSnapshot
from algo_trading.persistence.models.data_snapshot import DataSnapshotModel
from algo_trading.persistence.repositories.data_snapshot_repository import (
    DataSnapshotRepository,
    DuplicateDataSnapshotError,
)

DATABASE_URL = (
    "mysql+pymysql://algo_trading_app:"
    "algo-trading-dev-password@localhost:3306/algo_trading"
)

SNAPSHOT_ID = "integration-test:fake:1d:2024-01-01:2024-01-31"


def build_snapshot() -> DataSnapshot:
    return DataSnapshot(
        snapshot_id=SNAPSHOT_ID,
        source="integration-test",
        retrieved_at=datetime(2024, 1, 31, 22, 0, tzinfo=UTC),
        timeframe="1d",
        bars_count=20,
        splits_count=1,
        dividends_count=2,
        checksum="integration-test-checksum",
    )


def test_persists_and_retrieves_data_snapshot() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with Session(engine) as session:
        session.execute(
            delete(DataSnapshotModel).where(
                DataSnapshotModel.snapshot_id == SNAPSHOT_ID
            )
        )
        session.commit()

        repository = DataSnapshotRepository(session)
        repository.add(build_snapshot())
        repository.flush()
        session.commit()

        persisted_snapshot = repository.get_by_snapshot_id(SNAPSHOT_ID)

        assert persisted_snapshot is not None
        assert persisted_snapshot.snapshot_id == SNAPSHOT_ID
        assert persisted_snapshot.source == "integration-test"
        assert persisted_snapshot.retrieved_at == datetime(2024, 1, 31, 22, 0)
        assert persisted_snapshot.timeframe == "1d"
        assert persisted_snapshot.bars_count == 20
        assert persisted_snapshot.splits_count == 1
        assert persisted_snapshot.dividends_count == 2
        assert persisted_snapshot.checksum == "integration-test-checksum"

        session.execute(
            delete(DataSnapshotModel).where(
                DataSnapshotModel.snapshot_id == SNAPSHOT_ID
            )
        )
        session.commit()

    engine.dispose()


def test_rejects_duplicate_snapshot_id() -> None:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)

    with Session(engine) as session:
        session.execute(
            delete(DataSnapshotModel).where(
                DataSnapshotModel.snapshot_id == SNAPSHOT_ID
            )
        )
        session.commit()

        repository = DataSnapshotRepository(session)
        repository.add(build_snapshot())
        repository.flush()
        session.commit()

        repository.add(build_snapshot())

        with pytest.raises(DuplicateDataSnapshotError):
            repository.flush()

        session.execute(
            delete(DataSnapshotModel).where(
                DataSnapshotModel.snapshot_id == SNAPSHOT_ID
            )
        )
        session.commit()

    engine.dispose()
