"""add snapshot id to market data

Revision ID: 8b2c3d4e5f6a
Revises: 7a1b2c3d4e5f
Create Date: 2026-09-18 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b2c3d4e5f6a"
down_revision: str | Sequence[str] | None = "7a1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add snapshot lineage columns to raw data and corporate actions."""
    op.add_column(
        "market_bars_raw",
        sa.Column("snapshot_id", sa.String(length=128), nullable=True),
    )
    op.create_foreign_key(
        "fk_market_bars_raw_snapshot_id",
        "market_bars_raw",
        "data_snapshots",
        ["snapshot_id"],
        ["snapshot_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_market_bars_raw_snapshot_id",
        "market_bars_raw",
        ["snapshot_id"],
        unique=False,
    )

    op.add_column(
        "corporate_actions",
        sa.Column("snapshot_id", sa.String(length=128), nullable=True),
    )
    op.create_foreign_key(
        "fk_corporate_actions_snapshot_id",
        "corporate_actions",
        "data_snapshots",
        ["snapshot_id"],
        ["snapshot_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_corporate_actions_snapshot_id",
        "corporate_actions",
        ["snapshot_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove snapshot lineage columns from raw data and corporate actions."""
    op.drop_index("ix_corporate_actions_snapshot_id", table_name="corporate_actions")
    op.drop_constraint(
        "fk_corporate_actions_snapshot_id",
        "corporate_actions",
        type_="foreignkey",
    )
    op.drop_column("corporate_actions", "snapshot_id")

    op.drop_index("ix_market_bars_raw_snapshot_id", table_name="market_bars_raw")
    op.drop_constraint(
        "fk_market_bars_raw_snapshot_id",
        "market_bars_raw",
        type_="foreignkey",
    )
    op.drop_column("market_bars_raw", "snapshot_id")
