import random
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from algo_trading.analytics.report import build_report, report_to_json
from algo_trading.backtesting import BacktestEngine, BacktestParameters
from algo_trading.data.market_bar import MarketBar
from algo_trading.engine import IntrabarResolution, IntrabarResolver
from algo_trading.execution import ExitReason
from algo_trading.persistence.repositories.backtest_run_repository import (
    _run_parameters,
)
from algo_trading.portfolio import RiskParameters

START = datetime(2024, 1, 1, tzinfo=UTC)
# Niveles estrechos (0,5 × ATR) para que muchas velas toquen SL y TP.
PARAMETERS = BacktestParameters(
    initial_cash=Decimal("100000"),
    risk=RiskParameters(
        commission_rate=Decimal("0.0005"),
        stop_loss_atr_multiple=Decimal("0.5"),
        take_profit_atr_multiple=Decimal("0.5"),
    ),
)


def volatile_bars(symbol: str, seed: int, length: int = 500) -> list[MarketBar]:
    """Velas con rango amplio para que muchas toquen SL y TP a la vez."""
    generator = random.Random(seed)
    close = Decimal("100")
    bars = []

    for day in range(length):
        open_price = max(
            Decimal("20"), close + Decimal(generator.randint(-60, 80)) / 100
        )
        close = max(
            Decimal("20"), open_price + Decimal(generator.randint(-200, 240)) / 100
        )
        spread = Decimal(generator.choice([50, 100, 400, 700])) / 100
        bars.append(
            MarketBar(
                symbol=symbol,
                timestamp=START + timedelta(days=day),
                timeframe="1d",
                source="test",
                open=open_price,
                high=max(open_price, close) + spread,
                low=max(Decimal("1"), min(open_price, close) - spread),
                close=close,
                volume=Decimal("1000"),
            )
        )

    return bars


def universe() -> dict[str, list[MarketBar]]:
    return {"AAA": volatile_bars("AAA", 1), "BBB": volatile_bars("BBB", 2)}


class TakeProfitFirstSource:
    """Trayectoria sintética: máximo diario en el minuto 10, mínimo en el 45."""

    def __init__(self, bars: dict[str, list[MarketBar]]) -> None:
        self._daily = {
            (symbol, bar.timestamp.date()): bar
            for symbol, series in bars.items()
            for bar in series
        }
        self.calls = 0

    def get_session_bars(self, symbol: str, session_date: date) -> Sequence[MarketBar]:
        self.calls += 1
        daily = self._daily[(symbol, session_date)]
        session_open = datetime.combine(session_date, datetime.min.time(), UTC)

        def at(minute: int, low: Decimal, high: Decimal) -> MarketBar:
            return MarketBar(
                symbol=symbol,
                timestamp=session_open + timedelta(hours=14, minutes=30 + minute),
                timeframe="1m",
                source="test",
                open=low,
                high=high,
                low=low,
                close=low,
                volume=Decimal("100"),
            )

        return (
            at(0, daily.open, daily.open),
            at(10, daily.open, daily.high),
            at(45, daily.low, daily.low),
        )


def test_without_resolver_engine_is_v1() -> None:
    result = BacktestEngine(PARAMETERS).run(universe())

    assert not result.uses_intrabar_resolution
    assert result.intrabar_resolutions == ()
    assert "intrabar" not in report_to_json(build_report(result, universe()))


def test_resolver_without_source_keeps_v1_trades_and_records_conflicts() -> None:
    bars = universe()
    v1 = BacktestEngine(PARAMETERS).run(bars)

    result = BacktestEngine(PARAMETERS).run(
        bars, intrabar_resolver=IntrabarResolver(None)
    )

    assert result.trades == v1.trades
    assert result.equity_curve == v1.equity_curve
    assert result.uses_intrabar_resolution
    assert result.intrabar_resolutions
    assert {r.resolution for r in result.intrabar_resolutions} <= {
        IntrabarResolution.FALLBACK_NO_SOURCE,
        IntrabarResolution.OPENING_GAP,
    }


def test_intraday_path_turns_conflicts_into_take_profits() -> None:
    bars = universe()
    source = TakeProfitFirstSource(bars)

    result = BacktestEngine(PARAMETERS).run(
        bars, intrabar_resolver=IntrabarResolver(source)
    )
    resolved = [
        r
        for r in result.intrabar_resolutions
        if r.resolution is IntrabarResolution.INTRADAY_PATH
    ]

    assert resolved
    assert all(r.exit_reason is ExitReason.TAKE_PROFIT for r in resolved)
    assert source.calls == len(
        [
            r
            for r in result.intrabar_resolutions
            if r.resolution is not IntrabarResolution.OPENING_GAP
        ]
    )

    trades = {(trade.symbol, trade.exit_timestamp): trade for trade in result.trades}
    for record in resolved:
        trade = trades[(record.symbol, record.session_timestamp)]
        assert trade.exit_reason is ExitReason.TAKE_PROFIT
        assert trade.exit_theoretical_price == record.take_profit

    report = build_report(result, bars)
    summary = report.intrabar
    assert summary is not None
    assert summary.conflicts == len(result.intrabar_resolutions)
    assert summary.take_profit_first >= len(resolved)
    assert summary.fallbacks == 0
    assert summary.by_resolution["INTRADAY_PATH"] == len(resolved)
    assert "intrabar_resolution" in _run_parameters(report)
    assert "records" not in _run_parameters(report)["intrabar_resolution"]
    assert type(report).model_validate_json(report_to_json(report)) == report
