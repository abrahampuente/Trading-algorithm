from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from algo_trading.persistence.models.base import Base


class MarketBarRaw(Base):
    __tablename__ = "market_bars_raw"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)

    open: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)

    ingested_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "timestamp",
            "timeframe",
            "source",
            name="uq_market_bars_raw_identity",
        ),
        Index(
            "ix_market_bars_raw_symbol_timestamp",
            "symbol",
            "timestamp",
        ),
    )
