"""add sweep_id to backtest runs

Revision ID: a1d2e3f4b5c6
Revises: 9c4d5e6f7a8b
Create Date: 2026-10-23 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1d2e3f4b5c6"
down_revision: str | Sequence[str] | None = "9c4d5e6f7a8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Group backtest runs that belong to the same parameter sweep."""
    op.add_column(
        "backtest_runs",
        sa.Column("sweep_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_backtest_runs_sweep_id", "backtest_runs", ["sweep_id"])


def downgrade() -> None:
    """Remove sweep_id from backtest_runs."""
    op.drop_index("ix_backtest_runs_sweep_id", table_name="backtest_runs")
    op.drop_column("backtest_runs", "sweep_id")
