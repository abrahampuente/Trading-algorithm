"""create live state tables

Revision ID: c2d3e4f5a6b7
Revises: b7c8d9e0f1a2
Create Date: 2026-10-23 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(precision=20, scale=8)
ATR = sa.Numeric(precision=28, scale=12)
MULTIPLE = sa.Numeric(precision=10, scale=4)
BPS = sa.Numeric(precision=20, scale=4)
TIMESTAMP = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    """Pending protections, operational alerts and daily metrics (Hito 4)."""
    op.create_table(
        "pending_protections",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_client_order_id", sa.String(length=128), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("entry_atr", ATR, nullable=False),
        sa.Column("stop_loss_atr_multiple", MULTIPLE, nullable=False),
        sa.Column("take_profit_atr_multiple", MULTIPLE, nullable=False),
        sa.Column("signal_timestamp", TIMESTAMP, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("resolved_at", TIMESTAMP, nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=True),
        sa.Column("exit_client_order_id", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "entry_client_order_id",
            name="uq_pending_protections_entry_client_order_id",
        ),
    )
    op.create_index(
        "ix_pending_protections_resolution", "pending_protections", ["resolution"]
    )

    op.create_table(
        "operational_alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_operational_alerts_created_at", "operational_alerts", ["created_at"]
    )
    op.create_index(
        "ix_operational_alerts_code_created_at",
        "operational_alerts",
        ["code", "created_at"],
    )

    op.create_table(
        "daily_operational_metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("computed_at", TIMESTAMP, nullable=False),
        sa.Column("slippage_orders", sa.Integer(), nullable=False),
        sa.Column("slippage_mean_bps", BPS, nullable=True),
        sa.Column("slippage_weighted_bps", BPS, nullable=True),
        sa.Column("slippage_total_cost", MONEY, nullable=False),
        sa.Column("api_requests", sa.Integer(), nullable=False),
        sa.Column("api_errors", sa.Integer(), nullable=False),
        sa.Column("latency_mean_ms", sa.Float(), nullable=True),
        sa.Column("latency_p50_ms", sa.Float(), nullable=True),
        sa.Column("latency_p95_ms", sa.Float(), nullable=True),
        sa.Column("orders_total", sa.Integer(), nullable=False),
        sa.Column("orders_open", sa.Integer(), nullable=False),
        sa.Column("fill_rate", sa.Float(), nullable=True),
        sa.Column("reconciliation_consistent", sa.Boolean(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_date", name="uq_daily_operational_metrics_session_date"
        ),
    )


def downgrade() -> None:
    """Drop the live state tables (and their indexes)."""
    op.drop_table("daily_operational_metrics")
    op.drop_table("operational_alerts")
    op.drop_table("pending_protections")
