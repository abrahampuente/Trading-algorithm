"""create data snapshots

Revision ID: 7a1b2c3d4e5f
Revises: 51274cdec003
Create Date: 2026-09-12 01:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7a1b2c3d4e5f"
down_revision: str | Sequence[str] | None = "51274cdec003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create data_snapshots table."""
    op.create_table(
        "data_snapshots",
        sa.Column("snapshot_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("bars_count", sa.Integer(), nullable=False),
        sa.Column("splits_count", sa.Integer(), nullable=False),
        sa.Column("dividends_count", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=128), nullable=False),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )


def downgrade() -> None:
    """Drop data_snapshots table."""
    op.drop_table("data_snapshots")
