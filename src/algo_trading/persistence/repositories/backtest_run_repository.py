from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from algo_trading.analytics.report import BacktestReport
from algo_trading.persistence.models.backtest import (
    BacktestEquitySnapshotModel,
    BacktestRunModel,
    BacktestTradeModel,
)

MONEY_QUANTUM = Decimal("0.00000001")
RATIO_QUANTUM = Decimal("0.0000000001")


class BacktestRunMetadata(BaseModel):
    """Datos de la ejecución que no forman parte del informe determinista."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    created_at: datetime
    report_checksum: str
    source: str
    adjustment_version: str
    start_date: date
    end_date: date
    sweep_id: str | None = None


class BacktestRunSummary(BaseModel):
    """Resumen de una ejecución para compararla con otras."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    created_at: datetime
    report_checksum: str
    sweep_id: str | None
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    trading_calendar: str | None
    parameters: dict[str, Any]
    initial_cash: Decimal
    final_equity: Decimal
    cumulative_return: Decimal
    max_drawdown_pct: Decimal
    dividend_income: Decimal
    total_costs: Decimal
    total_trades: int
    win_rate: Decimal | None
    profit_factor: Decimal | None
    expectancy_r: Decimal | None
    buy_and_hold_return: Decimal | None
    excess_return: Decimal | None


class BacktestRunRepository:
    """
    Persistencia de ejecuciones de backtest.

    Guarda la ejecución (`backtest_runs`), sus trades (`backtest_trades`) y su
    curva de equity (`backtest_equity_snapshots`). No confirma la
    transacción. Los importes se redondean a 8 decimales y las ratios a 10,
    la escala de las columnas.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        report: BacktestReport,
        metadata: BacktestRunMetadata,
    ) -> BacktestRunModel:
        metrics = report.metrics
        trades = metrics.trades
        buy_and_hold = metrics.buy_and_hold

        run = BacktestRunModel(
            run_id=metadata.run_id,
            created_at=_naive_utc(metadata.created_at),
            report_version=report.report_version,
            report_checksum=metadata.report_checksum,
            sweep_id=metadata.sweep_id,
            symbols=",".join(report.symbols),
            start_date=metadata.start_date,
            end_date=metadata.end_date,
            source=metadata.source,
            adjustment_version=metadata.adjustment_version,
            trading_calendar=report.trading_calendar,
            parameters=_run_parameters(report),
            initial_cash=_money(metrics.initial_equity),
            final_equity=_money(metrics.final_equity),
            net_profit=_money(metrics.net_profit),
            cumulative_return=_ratio(metrics.cumulative_return),
            max_drawdown_pct=_ratio(metrics.max_drawdown.max_drawdown_pct),
            dividend_income=_money(metrics.dividend_income),
            total_costs=_money(trades.total_costs),
            total_trades=trades.total_trades,
            win_rate=_optional_ratio(trades.win_rate),
            profit_factor=_optional_ratio(trades.profit_factor),
            expectancy=_optional_money(trades.expectancy),
            expectancy_r=_optional_ratio(trades.expectancy_r),
            time_in_market=_optional_ratio(metrics.exposure.time_in_market),
            gap_filtered_entries=metrics.gap_filtered_entries,
            rejected_entries=metrics.rejected_entries,
            buy_and_hold_return=_optional_ratio(
                buy_and_hold.cumulative_return if buy_and_hold else None
            ),
            buy_and_hold_max_drawdown_pct=_optional_ratio(
                buy_and_hold.max_drawdown.max_drawdown_pct if buy_and_hold else None
            ),
            excess_return=_optional_ratio(
                buy_and_hold.strategy_excess_return if buy_and_hold else None
            ),
        )
        self._session.add(run)
        self._session.flush()

        if report.trades:
            self._session.execute(
                insert(BacktestTradeModel),
                [
                    {
                        "run_id": metadata.run_id,
                        "symbol": trade.symbol,
                        "signal_timestamp": _naive_utc(trade.signal_timestamp),
                        "entry_timestamp": _naive_utc(trade.entry_timestamp),
                        "exit_timestamp": _naive_utc(trade.exit_timestamp),
                        "sessions_since_entry": trade.sessions_since_entry,
                        "quantity": trade.quantity,
                        "entry_theoretical_price": _money(
                            trade.entry_theoretical_price
                        ),
                        "entry_executed_price": _money(trade.entry_executed_price),
                        "exit_theoretical_price": _money(trade.exit_theoretical_price),
                        "exit_executed_price": _money(trade.exit_executed_price),
                        "entry_atr": _money(trade.entry_atr),
                        "stop_loss": _money(trade.stop_loss),
                        "take_profit": _money(trade.take_profit),
                        "planned_risk": _money(trade.planned_risk),
                        "entry_commission": _money(trade.entry_commission),
                        "exit_commission": _money(trade.exit_commission),
                        "slippage_cost": _money(trade.slippage_cost),
                        "total_costs": _money(trade.total_costs),
                        "dividend_income": _money(trade.dividend_income),
                        "gross_pnl": _money(trade.gross_pnl),
                        "net_pnl": _money(trade.net_pnl),
                        "return_pct": _ratio(trade.return_pct),
                        "exit_reason": str(trade.exit_reason),
                    }
                    for trade in report.trades
                ],
            )

        if report.equity_curve:
            self._session.execute(
                insert(BacktestEquitySnapshotModel),
                [
                    {
                        "run_id": metadata.run_id,
                        "timestamp": _naive_utc(point.timestamp),
                        "cash": _money(point.cash),
                        "market_value": _money(point.market_value),
                        "equity": _money(point.equity),
                        "open_positions": point.open_positions,
                    }
                    for point in report.equity_curve
                ],
            )

        return run

    def get_summary(self, run_id: str) -> BacktestRunSummary | None:
        run = self._session.get(BacktestRunModel, run_id)
        return _to_summary(run) if run is not None else None

    def list_summaries(
        self,
        limit: int = 20,
        sweep_id: str | None = None,
    ) -> tuple[BacktestRunSummary, ...]:
        """Ejecuciones más recientes primero; opcionalmente de un barrido."""
        statement = select(BacktestRunModel)

        if sweep_id is not None:
            statement = statement.where(BacktestRunModel.sweep_id == sweep_id)

        statement = statement.order_by(
            BacktestRunModel.created_at.desc(),
            BacktestRunModel.run_id,
        ).limit(limit)
        return tuple(_to_summary(run) for run in self._session.scalars(statement))

    def get_trades(self, run_id: str) -> Sequence[BacktestTradeModel]:
        statement = (
            select(BacktestTradeModel)
            .where(BacktestTradeModel.run_id == run_id)
            .order_by(BacktestTradeModel.id)
        )
        return self._session.scalars(statement).all()

    def get_equity_curve(self, run_id: str) -> Sequence[BacktestEquitySnapshotModel]:
        statement = (
            select(BacktestEquitySnapshotModel)
            .where(BacktestEquitySnapshotModel.run_id == run_id)
            .order_by(BacktestEquitySnapshotModel.timestamp)
        )
        return self._session.scalars(statement).all()


def _to_summary(run: BacktestRunModel) -> BacktestRunSummary:
    return BacktestRunSummary(
        run_id=run.run_id,
        created_at=run.created_at.replace(tzinfo=UTC),
        report_checksum=run.report_checksum,
        sweep_id=run.sweep_id,
        symbols=tuple(run.symbols.split(",")),
        start_date=run.start_date,
        end_date=run.end_date,
        trading_calendar=run.trading_calendar,
        parameters=run.parameters,
        initial_cash=run.initial_cash,
        final_equity=run.final_equity,
        cumulative_return=run.cumulative_return,
        max_drawdown_pct=run.max_drawdown_pct,
        dividend_income=run.dividend_income,
        total_costs=run.total_costs,
        total_trades=run.total_trades,
        win_rate=run.win_rate,
        profit_factor=run.profit_factor,
        expectancy_r=run.expectancy_r,
        buy_and_hold_return=run.buy_and_hold_return,
        excess_return=run.excess_return,
    )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value

    return value.astimezone(UTC).replace(tzinfo=None)


def _run_parameters(report: BacktestReport) -> dict[str, Any]:
    """Parámetros del backtest; con filtros v2 añade `signal_filters`."""
    parameters = report.parameters.model_dump(mode="json")

    if report.signal_filters is not None:
        parameters["signal_filters"] = report.signal_filters.model_dump(mode="json")

    if report.intrabar is not None:
        parameters["intrabar_resolution"] = report.intrabar.model_dump(
            mode="json", exclude={"records"}
        )

    return parameters


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(RATIO_QUANTUM, rounding=ROUND_HALF_EVEN)


def _optional_money(value: Decimal | None) -> Decimal | None:
    return _money(value) if value is not None else None


def _optional_ratio(value: Decimal | None) -> Decimal | None:
    return _ratio(value) if value is not None else None
