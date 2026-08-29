from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from algo_trading.persistence.models.base import Base


class CorporateActionType(StrEnum):
    SPLIT = "split"
    DIVIDEND = "dividend"


class CorporateAction(Base):
    __tablename__ = "corporate_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    action_type: Mapped[CorporateActionType] = mapped_column(
        String(16),
        nullable=False,
    )
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)

    split_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8),
        nullable=True,
    )
    dividend_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8),
        nullable=True,
    )

    source: Mapped[str] = mapped_column(String(64), nullable=False)
    announced_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC).replace(tzinfo=None),
    )

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "action_type",
            "ex_date",
            "source",
            name="uq_corporate_actions_identity",
        ),
        Index(
            "ix_corporate_actions_symbol_ex_date",
            "symbol",
            "ex_date",
        ),
    )
