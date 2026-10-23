from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from algo_trading.persistence.models.base import Base

MONEY = Numeric(20, 8)
RATIO = Numeric(20, 10)


class BacktestRunModel(Base):
    """Ejecución de backtest: parámetros completos y métricas principales."""

    __tablename__ = "backtest_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    report_version: Mapped[str] = mapped_column(String(32), nullable=False)
    report_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    sweep_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    symbols: Mapped[str] = mapped_column(String(512), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    adjustment_version: Mapped[str] = mapped_column(String(64), nullable=False)
    trading_calendar: Mapped[str | None] = mapped_column(String(32), nullable=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    initial_cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    final_equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    net_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cumulative_return: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    dividend_income: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_costs: Mapped[Decimal] = mapped_column(MONEY, nullable=False)

    total_trades: Mapped[int] = mapped_column(Integer, nullable=False)
    win_rate: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    profit_factor: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    expectancy: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    expectancy_r: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    time_in_market: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    gap_filtered_entries: Mapped[int] = mapped_column(Integer, nullable=False)
    rejected_entries: Mapped[int] = mapped_column(Integer, nullable=False)

    buy_and_hold_return: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)
    buy_and_hold_max_drawdown_pct: Mapped[Decimal | None] = mapped_column(
        RATIO,
        nullable=True,
    )
    excess_return: Mapped[Decimal | None] = mapped_column(RATIO, nullable=True)

    __table_args__ = (
        Index("ix_backtest_runs_created_at", "created_at"),
        Index("ix_backtest_runs_sweep_id", "sweep_id"),
    )


class BacktestTradeModel(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("backtest_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    entry_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    exit_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sessions_since_entry: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    entry_theoretical_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    entry_executed_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    exit_theoretical_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    exit_executed_price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    entry_atr: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    stop_loss: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    take_profit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    planned_risk: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    entry_commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    exit_commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    slippage_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_costs: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    dividend_income: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    return_pct: Mapped[Decimal] = mapped_column(RATIO, nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (Index("ix_backtest_trades_run_symbol", "run_id", "symbol"),)


class BacktestEquitySnapshotModel(Base):
    __tablename__ = "backtest_equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("backtest_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    cash: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    market_value: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    equity: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "timestamp",
            name="uq_backtest_equity_snapshots_run_timestamp",
        ),
    )
