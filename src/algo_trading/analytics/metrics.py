from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, localcontext

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.backtesting.engine import (
    BacktestResult,
    EquityPoint,
    ExecutionBar,
)
from algo_trading.domain.corporate_actions.dto import CorporateDividend
from algo_trading.execution.costs import CostModel
from algo_trading.execution.trade import Trade
from algo_trading.portfolio.position_sizing import MONEY_PRECISION, MONEY_ROUNDING

ZERO = Decimal("0")


class TradeStatistics(BaseModel):
    """
    Estadísticas sobre trades cerrados, calculadas con el PnL neto.

    Un trade es ganador si `net_pnl > 0`; el resto cuenta como perdedor.
    Las ratios sin denominador válido son None (p. ej. sin trades, o
    `profit_factor` sin pérdidas).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_trades: int = Field(ge=0)
    winning_trades: int = Field(ge=0)
    losing_trades: int = Field(ge=0)
    win_rate: Decimal | None
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    expectancy_r: Decimal | None
    average_win: Decimal | None
    average_loss: Decimal | None
    average_sessions_held: Decimal | None
    average_holding_days: Decimal | None
    total_costs: Decimal
    exits_by_reason: dict[str, int]


class Drawdown(BaseModel):
    """Máxima caída porcentual desde un máximo previo de la curva de equity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_drawdown_pct: Decimal
    max_drawdown_amount: Decimal
    peak_timestamp: datetime | None
    trough_timestamp: datetime | None


class Exposure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    time_in_market: Decimal | None
    average_exposure: Decimal | None


class BuyAndHoldComparison(BaseModel):
    """
    Referencia Buy & Hold equiponderada sobre los mismos símbolos.

    El capital inicial se reparte a partes iguales; cada símbolo se compra en
    la apertura de su primera barra y se vende al último cierre, con los
    mismos costes por ejecución que la estrategia (unidades fraccionarias).
    Los dividendos con ex_date posterior a la compra y hasta la última barra
    se acreditan en cash (no se reinvierten).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_equity: Decimal
    final_equity: Decimal
    cumulative_return: Decimal
    dividend_income: Decimal = Decimal("0")
    max_drawdown: Drawdown
    strategy_excess_return: Decimal


class PerformanceMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_equity: Decimal
    final_equity: Decimal
    net_profit: Decimal
    cumulative_return: Decimal
    dividend_income: Decimal = Decimal("0")
    trades: TradeStatistics
    max_drawdown: Drawdown
    exposure: Exposure
    buy_and_hold: BuyAndHoldComparison | None
    gap_filtered_entries: int = Field(ge=0)
    rejected_entries: int = Field(ge=0)


def calculate_trade_statistics(trades: Sequence[Trade]) -> TradeStatistics:
    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
        losses = [trade.net_pnl for trade in trades if trade.net_pnl <= 0]
        gross_profit = sum(wins, ZERO)
        gross_loss = -sum(losses, ZERO)
        total = len(trades)

        return TradeStatistics(
            total_trades=total,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=Decimal(len(wins)) / total if total else None,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=gross_profit / gross_loss if gross_loss > 0 else None,
            expectancy=_mean([trade.net_pnl for trade in trades]),
            expectancy_r=_mean(
                [
                    trade.net_pnl / trade.planned_risk
                    for trade in trades
                    if trade.planned_risk > 0
                ]
            ),
            average_win=_mean(wins),
            average_loss=_mean(losses),
            average_sessions_held=_mean(
                [Decimal(trade.sessions_since_entry) for trade in trades]
            ),
            average_holding_days=_mean(
                [
                    Decimal((trade.exit_timestamp - trade.entry_timestamp).days)
                    for trade in trades
                ]
            ),
            total_costs=sum((trade.total_costs for trade in trades), ZERO),
            exits_by_reason=dict(
                sorted(Counter(str(trade.exit_reason) for trade in trades).items())
            ),
        )


def calculate_max_drawdown(
    equity_points: Sequence[tuple[datetime, Decimal]],
    initial_equity: Decimal,
) -> Drawdown:
    """
    Calcula el Maximum Drawdown.

    `drawdown[t] = (máximo previo - equity[t]) / máximo previo`. El capital
    inicial actúa como primer máximo.
    """
    peak = initial_equity
    peak_timestamp: datetime | None = None
    worst = Drawdown(
        max_drawdown_pct=ZERO,
        max_drawdown_amount=ZERO,
        peak_timestamp=None,
        trough_timestamp=None,
    )

    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        for timestamp, equity in equity_points:
            if equity > peak:
                peak = equity
                peak_timestamp = timestamp
                continue

            if peak <= 0:
                continue

            drawdown_pct = (peak - equity) / peak

            if drawdown_pct > worst.max_drawdown_pct:
                worst = Drawdown(
                    max_drawdown_pct=drawdown_pct,
                    max_drawdown_amount=peak - equity,
                    peak_timestamp=peak_timestamp,
                    trough_timestamp=timestamp,
                )

    return worst


def calculate_exposure(result: BacktestResult) -> Exposure:
    return calculate_curve_exposure(result.equity_curve)


def calculate_curve_exposure(points: Sequence[EquityPoint]) -> Exposure:
    """Tiempo en mercado y exposición media (`valor_mercado / equity`)."""
    if not points:
        return Exposure(time_in_market=None, average_exposure=None)

    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        in_market = sum(1 for point in points if point.open_positions > 0)
        exposures = [
            point.market_value / point.equity for point in points if point.equity > 0
        ]

        return Exposure(
            time_in_market=Decimal(in_market) / len(points),
            average_exposure=_mean(exposures),
        )


def calculate_buy_and_hold(
    bars_by_symbol: Mapping[str, Sequence[ExecutionBar]],
    initial_equity: Decimal,
    costs: CostModel,
    strategy_return: Decimal,
    dividends_by_symbol: Mapping[str, Sequence[CorporateDividend]] | None = None,
) -> BuyAndHoldComparison | None:
    series = {symbol: bars for symbol, bars in bars_by_symbol.items() if bars}

    if not series:
        return None

    dividends = dividends_by_symbol or {}
    first_dates = {symbol: bars[0].timestamp.date() for symbol, bars in series.items()}
    last_dates = {symbol: bars[-1].timestamp.date() for symbol, bars in series.items()}

    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        allocation = initial_equity / len(series)
        units = {
            symbol: allocation
            / (costs.buy_price(bars[0].open) * (1 + costs.commission_rate))
            for symbol, bars in series.items()
        }
        closes_by_session: dict[datetime, dict[str, Decimal]] = {}

        for symbol, bars in series.items():
            for bar in bars:
                closes_by_session.setdefault(bar.timestamp, {})[symbol] = bar.close

        last_close: dict[str, Decimal] = {}
        equity_points: list[tuple[datetime, Decimal]] = []
        dividend_cash = ZERO
        previous_date: date | None = None

        for session in sorted(closes_by_session):
            session_date = session.date()
            last_close.update(closes_by_session[session])

            for symbol in series:
                for dividend in dividends.get(symbol, ()):
                    is_new = previous_date is None or dividend.ex_date > previous_date

                    if (
                        is_new
                        and dividend.ex_date <= session_date
                        and first_dates[symbol] < dividend.ex_date
                        and dividend.ex_date <= last_dates[symbol]
                    ):
                        dividend_cash += units[symbol] * dividend.amount

            previous_date = session_date
            equity = dividend_cash + sum(
                (
                    units[symbol] * last_close[symbol]
                    if symbol in last_close
                    else allocation
                    for symbol in series
                ),
                ZERO,
            )
            equity_points.append((session, equity))

        final_equity = dividend_cash + sum(
            (
                units[symbol]
                * costs.sell_price(bars[-1].close)
                * (1 - costs.commission_rate)
                for symbol, bars in series.items()
            ),
            ZERO,
        )
        cumulative_return = final_equity / initial_equity - 1

        return BuyAndHoldComparison(
            initial_equity=initial_equity,
            final_equity=final_equity,
            cumulative_return=cumulative_return,
            dividend_income=dividend_cash,
            max_drawdown=calculate_max_drawdown(equity_points, initial_equity),
            strategy_excess_return=strategy_return - cumulative_return,
        )


def calculate_performance_metrics(
    result: BacktestResult,
    bars_by_symbol: Mapping[str, Sequence[ExecutionBar]] | None = None,
    dividends_by_symbol: Mapping[str, Sequence[CorporateDividend]] | None = None,
) -> PerformanceMetrics:
    """
    Calcula las métricas de un backtest.

    `final_equity` es el cash final, con todas las posiciones ya cerradas
    (incluidas las cerradas por `END_OF_DATA`). La comparación Buy & Hold
    sólo se calcula si se proporcionan las barras usadas en el backtest.
    """
    initial_equity = result.parameters.initial_cash
    final_equity = result.final_cash

    with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
        cumulative_return = final_equity / initial_equity - 1

    buy_and_hold = (
        calculate_buy_and_hold(
            bars_by_symbol,
            initial_equity=initial_equity,
            costs=result.parameters.costs,
            strategy_return=cumulative_return,
            dividends_by_symbol=dividends_by_symbol,
        )
        if bars_by_symbol is not None
        else None
    )

    return PerformanceMetrics(
        initial_equity=initial_equity,
        final_equity=final_equity,
        net_profit=final_equity - initial_equity,
        cumulative_return=cumulative_return,
        dividend_income=sum((payment.total for payment in result.dividends), ZERO),
        trades=calculate_trade_statistics(result.trades),
        max_drawdown=calculate_max_drawdown(
            [(point.timestamp, point.equity) for point in result.equity_curve],
            initial_equity,
        ),
        exposure=calculate_exposure(result),
        buy_and_hold=buy_and_hold,
        gap_filtered_entries=len(result.gap_filtered),
        rejected_entries=len(result.rejected),
    )


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None

    return sum(values, ZERO) / len(values)
