from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from algo_trading.persistence.models.base import Base


class DataSnapshotModel(Base):
    __tablename__ = "data_snapshots"

    snapshot_id: Mapped[str] = mapped_column(
        String(128),
        primary_key=True,
    )

    source: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )

    timeframe: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )

    bars_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    splits_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    dividends_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    checksum: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
