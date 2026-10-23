"""create broker orders and executions

Revision ID: b7c8d9e0f1a2
Revises: a1d2e3f4b5c6
Create Date: 2026-10-23 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "a1d2e3f4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(precision=20, scale=8)
QUANTITY = sa.Numeric(precision=20, scale=8)
TIMESTAMP = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    """Create broker_orders and order_executions (paper trading, Hito 3)."""
    op.create_table(
        "broker_orders",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("client_order_id", sa.String(length=128), nullable=False),
        sa.Column("broker_order_id", sa.String(length=64), nullable=False),
        sa.Column("parent_client_order_id", sa.String(length=128), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("quantity", QUANTITY, nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("time_in_force", sa.String(length=8), nullable=False),
        sa.Column("order_class", sa.String(length=8), nullable=False),
        sa.Column("limit_price", MONEY, nullable=True),
        sa.Column("stop_price", MONEY, nullable=True),
        sa.Column("take_profit_limit_price", MONEY, nullable=True),
        sa.Column("stop_loss_stop_price", MONEY, nullable=True),
        sa.Column("theoretical_price", MONEY, nullable=True),
        sa.Column("strategy_id", sa.String(length=64), nullable=True),
        sa.Column("signal_timestamp", TIMESTAMP, nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("broker_status", sa.String(length=32), nullable=False),
        sa.Column("filled_quantity", QUANTITY, nullable=False),
        sa.Column("filled_average_price", MONEY, nullable=True),
        sa.Column("created_at", TIMESTAMP, nullable=False),
        sa.Column("submitted_at", TIMESTAMP, nullable=True),
        sa.Column("filled_at", TIMESTAMP, nullable=True),
        sa.Column("broker_updated_at", TIMESTAMP, nullable=True),
        sa.Column("last_synced_at", TIMESTAMP, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id", name="uq_broker_orders_client_order_id"),
        sa.UniqueConstraint("broker_order_id", name="uq_broker_orders_broker_order_id"),
    )
    op.create_index("ix_broker_orders_status", "broker_orders", ["status"])
    op.create_index(
        "ix_broker_orders_symbol_created_at",
        "broker_orders",
        ["symbol", "created_at"],
    )
    op.create_index(
        "ix_broker_orders_parent_client_order_id",
        "broker_orders",
        ["parent_client_order_id"],
    )

    op.create_table(
        "order_executions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.String(length=160), nullable=False),
        sa.Column("executed_at", TIMESTAMP, nullable=False),
        sa.Column("quantity", QUANTITY, nullable=False),
        sa.Column("price", MONEY, nullable=False),
        sa.Column("cumulative_quantity", QUANTITY, nullable=False),
        sa.Column("commission", MONEY, nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["broker_orders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", name="uq_order_executions_execution_id"),
    )
    op.create_index(
        "ix_order_executions_order_id_executed_at",
        "order_executions",
        ["order_id", "executed_at"],
    )


def downgrade() -> None:
    """Drop order_executions and broker_orders (and their indexes)."""
    # MySQL usa el índice compuesto para la clave foránea: no se puede borrar
    # antes que la tabla; drop_table elimina también los índices.
    op.drop_table("order_executions")
    op.drop_table("broker_orders")
