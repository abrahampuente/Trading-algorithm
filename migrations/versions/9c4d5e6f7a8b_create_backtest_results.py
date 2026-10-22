"""create backtest results

Revision ID: 9c4d5e6f7a8b
Revises: 8b2c3d4e5f6a
Create Date: 2026-10-22 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c4d5e6f7a8b"
down_revision: str | Sequence[str] | None = "8b2c3d4e5f6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(precision=20, scale=8)
RATIO = sa.Numeric(precision=20, scale=10)


def upgrade() -> None:
    """Create backtest_runs, backtest_trades and backtest_equity_snapshots."""
    op.create_table(
        "backtest_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("report_version", sa.String(length=32), nullable=False),
        sa.Column("report_checksum", sa.String(length=64), nullable=False),
        sa.Column("symbols", sa.String(length=512), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("adjustment_version", sa.String(length=64), nullable=False),
        sa.Column("trading_calendar", sa.String(length=32), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("initial_cash", MONEY, nullable=False),
        sa.Column("final_equity", MONEY, nullable=False),
        sa.Column("net_profit", MONEY, nullable=False),
        sa.Column("cumulative_return", RATIO, nullable=False),
        sa.Column("max_drawdown_pct", RATIO, nullable=False),
        sa.Column("dividend_income", MONEY, nullable=False),
        sa.Column("total_costs", MONEY, nullable=False),
        sa.Column("total_trades", sa.Integer(), nullable=False),
        sa.Column("win_rate", RATIO, nullable=True),
        sa.Column("profit_factor", RATIO, nullable=True),
        sa.Column("expectancy", MONEY, nullable=True),
        sa.Column("expectancy_r", RATIO, nullable=True),
        sa.Column("time_in_market", RATIO, nullable=True),
        sa.Column("gap_filtered_entries", sa.Integer(), nullable=False),
        sa.Column("rejected_entries", sa.Integer(), nullable=False),
        sa.Column("buy_and_hold_return", RATIO, nullable=True),
        sa.Column("buy_and_hold_max_drawdown_pct", RATIO, nullable=True),
        sa.Column("excess_return", RATIO, nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_backtest_runs_created_at", "backtest_runs", ["created_at"])

    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("signal_timestamp", sa.DateTime(), nullable=False),
        sa.Column("entry_timestamp", sa.DateTime(), nullable=False),
        sa.Column("exit_timestamp", sa.DateTime(), nullable=False),
        sa.Column("sessions_since_entry", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("entry_theoretical_price", MONEY, nullable=False),
        sa.Column("entry_executed_price", MONEY, nullable=False),
        sa.Column("exit_theoretical_price", MONEY, nullable=False),
        sa.Column("exit_executed_price", MONEY, nullable=False),
        sa.Column("entry_atr", MONEY, nullable=False),
        sa.Column("stop_loss", MONEY, nullable=False),
        sa.Column("take_profit", MONEY, nullable=False),
        sa.Column("planned_risk", MONEY, nullable=False),
        sa.Column("entry_commission", MONEY, nullable=False),
        sa.Column("exit_commission", MONEY, nullable=False),
        sa.Column("slippage_cost", MONEY, nullable=False),
        sa.Column("total_costs", MONEY, nullable=False),
        sa.Column("dividend_income", MONEY, nullable=False),
        sa.Column("gross_pnl", MONEY, nullable=False),
        sa.Column("net_pnl", MONEY, nullable=False),
        sa.Column("return_pct", RATIO, nullable=False),
        sa.Column("exit_reason", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.run_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_backtest_trades_run_symbol",
        "backtest_trades",
        ["run_id", "symbol"],
    )

    op.create_table(
        "backtest_equity_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.Column("cash", MONEY, nullable=False),
        sa.Column("market_value", MONEY, nullable=False),
        sa.Column("equity", MONEY, nullable=False),
        sa.Column("open_positions", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["backtest_runs.run_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "timestamp",
            name="uq_backtest_equity_snapshots_run_timestamp",
        ),
    )


def downgrade() -> None:
    """Drop backtest result tables."""
    op.drop_table("backtest_equity_snapshots")
    op.drop_table("backtest_trades")
    op.drop_table("backtest_runs")
