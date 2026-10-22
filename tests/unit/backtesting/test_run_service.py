from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.backtesting import BacktestParameters
from algo_trading.backtesting.run_service import (
    BacktestRunRequest,
    BacktestRunService,
    MissingMarketDataError,
    default_report_name,
)
from algo_trading.cli.backtest import format_summary, parse_args, run_backtest_command
from algo_trading.execution import ExitReason
from algo_trading.persistence.models import Base
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted

START = datetime(2024, 1, 1)


def adjusted_row(
    index: int,
    ohlc: tuple[str, str, str, str],
    symbol: str = "AAA",
    source: str = "yfinance",
    adjustment_version: str = "splits-v1",
) -> MarketBarAdjusted:
    open_price, high, low, close = (Decimal(value) for value in ohlc)
    return MarketBarAdjusted(
        symbol=symbol,
        timestamp=START + timedelta(days=index),
        timeframe="1d",
        source=source,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=Decimal("1000"),
        adjustment_factor=Decimal("1"),
        adjustment_version=adjustment_version,
    )


def take_profit_rows(symbol: str = "AAA") -> list[MarketBarAdjusted]:
    """Señal en el índice 20, entrada en 21 y take profit en 22."""
    rows = []

    for index in range(20):
        close = Decimal("100") + Decimal("0.1") * index
        rows.append(
            adjusted_row(
                index,
                (str(close), str(close + 1), str(close - 1), str(close)),
                symbol,
            )
        )

    rows.extend(
        [
            adjusted_row(20, ("110", "111", "109", "110"), symbol),
            adjusted_row(21, ("110", "110.5", "109.5", "110"), symbol),
            adjusted_row(22, ("111", "115", "110.5", "114"), symbol),
        ]
    )
    return rows


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)

    with Session(database) as session:
        session.add_all(take_profit_rows())
        # Otra versión de ajuste y otra fuente: no deben mezclarse.
        session.add(adjusted_row(0, ("1", "1", "1", "1"), adjustment_version="v0"))
        session.add(adjusted_row(0, ("1", "1", "1", "1"), source="other"))
        session.commit()

    yield database
    database.dispose()


def request(
    end: date = date(2024, 1, 23),
    symbols: tuple[str, ...] = ("AAA",),
) -> BacktestRunRequest:
    return BacktestRunRequest(
        symbols=symbols,
        start=date(2024, 1, 1),
        end=end,
        parameters=BacktestParameters(initial_cash=Decimal("100000")),
    )


def test_loads_adjusted_bars_for_source_and_version_with_utc_timestamps(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        bars = BacktestRunService(session).load_bars(request())

    assert list(bars) == ["AAA"]
    assert len(bars["AAA"]) == 23
    assert bars["AAA"][0].close == Decimal("100")
    assert bars["AAA"][0].timestamp == datetime(2024, 1, 1, tzinfo=UTC)


def test_runs_backtest_and_builds_report(engine: Engine) -> None:
    with Session(engine) as session:
        report = BacktestRunService(session).run(request())

    assert report.symbols == ("AAA",)
    assert len(report.trades) == 1
    assert report.trades[0].exit_reason is ExitReason.TAKE_PROFIT
    assert report.metrics.trades.total_trades == 1
    assert report.metrics.buy_and_hold is not None


def test_end_date_is_inclusive(engine: Engine) -> None:
    with Session(engine) as session:
        bars = BacktestRunService(session).load_bars(request(end=date(2024, 1, 22)))
        report = BacktestRunService(session).run(request(end=date(2024, 1, 22)))

    assert bars["AAA"][-1].timestamp == datetime(2024, 1, 22, tzinfo=UTC)
    # Sin la barra del take profit, la posición se cierra por fin de datos.
    assert report.trades[0].exit_reason is ExitReason.END_OF_DATA


def test_missing_symbol_raises_with_symbol_list(engine: Engine) -> None:
    with (
        Session(engine) as session,
        pytest.raises(MissingMarketDataError, match="BBB, CCC"),
    ):
        BacktestRunService(session).run(request(symbols=("AAA", "BBB", "CCC")))


def test_request_rejects_inverted_period() -> None:
    with pytest.raises(ValueError, match="end"):
        request(end=date(2023, 12, 31))


def test_default_report_name_is_deterministic() -> None:
    assert (
        default_report_name(["MSFT", "AAPL"], date(2020, 1, 1), date(2026, 10, 21))
        == "backtest_AAPL-MSFT_2020-01-01_2026-10-21"
    )


def test_cli_writes_json_and_markdown_reports(engine: Engine, tmp_path: Path) -> None:
    arguments = parse_args(
        [
            "--symbols",
            "aaa",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-23",
            "--output-dir",
            str(tmp_path),
        ]
    )

    report, json_path, markdown_path = run_backtest_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        default_initial_cash=Decimal("50000"),
        max_positions=5,
    )

    assert report.parameters.initial_cash == Decimal("50000")
    assert json_path == tmp_path / "backtest_AAA_2024-01-01_2024-01-23.json"
    assert json_path.exists()
    assert markdown_path.read_text(encoding="utf-8").startswith("# Informe de backtest")
    summary = format_summary(report, json_path, markdown_path)
    assert "Trades: 1" in summary
    assert "Buy & Hold" in summary


def test_cli_defaults() -> None:
    arguments = parse_args([])

    assert arguments.symbols == ["AAPL", "MSFT", "GOOGL", "SPY", "QQQ"]
    assert arguments.start == date(2020, 1, 1)
    assert arguments.end is None
    assert arguments.initial_cash is None
    assert arguments.gap_threshold_atr == Decimal("0.5")
    assert arguments.adjustment_version == "splits-v1"
