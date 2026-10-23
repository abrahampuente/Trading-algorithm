import random
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.analytics.intrabar_comparison import intrabar_comparison_to_markdown
from algo_trading.cli import backtest as cli
from algo_trading.config.settings import ApplicationSettings
from algo_trading.data.market_bar import MarketBar
from algo_trading.engine import IntrabarResolver
from algo_trading.persistence.models import BacktestRunModel, Base
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted

START = datetime(2024, 1, 1)
ARGS = [
    "--intrabar-resolution",
    "--symbols",
    "AAA",
    "BBB",
    "--start",
    "2024-01-01",
    "--end",
    "2025-05-14",
    "--calendar",
    "none",
    "--stop-loss-atr",
    "0.5",
    "--take-profit-atr",
    "0.5",
]


def volatile_rows(symbol: str, seed: int) -> list[MarketBarAdjusted]:
    generator = random.Random(seed)
    close = Decimal("100")
    rows = []

    for day in range(500):
        open_price = max(
            Decimal("20"), close + Decimal(generator.randint(-60, 80)) / 100
        )
        close = max(
            Decimal("20"), open_price + Decimal(generator.randint(-200, 240)) / 100
        )
        spread = Decimal(generator.choice([50, 100, 400, 700])) / 100
        rows.append(
            MarketBarAdjusted(
                symbol=symbol,
                timestamp=START + timedelta(days=day),
                timeframe="1d",
                source="yfinance",
                open=open_price,
                high=max(open_price, close) + spread,
                low=max(Decimal("1"), min(open_price, close) - spread),
                close=close,
                volume=Decimal("1000"),
                adjustment_factor=Decimal("1"),
                adjustment_version="splits-v1",
            )
        )

    return rows


class TakeProfitFirstSource:
    """Máximo diario en el minuto 10 y mínimo en el 45 para cada sesión."""

    def __init__(self, rows: list[MarketBarAdjusted]) -> None:
        self._daily = {(row.symbol, row.timestamp.date()): row for row in rows}

    def get_session_bars(self, symbol: str, session_date: date) -> Sequence[MarketBar]:
        daily = self._daily[(symbol, session_date)]
        session_open = datetime.combine(session_date, datetime.min.time(), UTC)

        def at(minute: int, price_low: Decimal, price_high: Decimal) -> MarketBar:
            return MarketBar(
                symbol=symbol,
                timestamp=session_open + timedelta(hours=14, minutes=30 + minute),
                timeframe="1m",
                source="test",
                open=price_low,
                high=price_high,
                low=price_low,
                close=price_low,
                volume=Decimal("100"),
            )

        return (
            at(0, daily.open, daily.open),
            at(10, daily.open, daily.high),
            at(45, daily.low, daily.low),
        )


@pytest.fixture
def rows() -> list[MarketBarAdjusted]:
    return volatile_rows("AAA", 1) + volatile_rows("BBB", 2)


@pytest.fixture
def engine(rows: list[MarketBarAdjusted]) -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)

    with Session(database) as session:
        # Copias: los originales los sigue leyendo la fuente intradía falsa y
        # no deben quedar expirados por el commit.
        session.add_all([row.__class__(**_columns(row)) for row in rows])
        session.commit()

    yield database
    database.dispose()


def _columns(row: MarketBarAdjusted) -> dict[str, object]:
    return {
        name: getattr(row, name)
        for name in (
            "symbol",
            "timestamp",
            "timeframe",
            "source",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "adjustment_factor",
            "adjustment_version",
        )
    }


def test_comparison_runs_v1_and_intrabar_on_the_same_data(
    engine: Engine,
    rows: list[MarketBarAdjusted],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "build_intrabar_resolver",
        lambda arguments, settings, calendar: IntrabarResolver(
            TakeProfitFirstSource(rows)
        ),
    )
    arguments = cli.parse_args([*ARGS, "--output-dir", str(tmp_path)])

    outcome, json_path, _, comparison, comparison_path = (
        cli.run_intrabar_comparison_command(
            arguments,
            session_factory=sessionmaker(bind=engine),
            settings=ApplicationSettings.from_yaml(),
            calendar=None,
        )
    )

    assert json_path.name == "backtest_intrabar_AAA-BBB_2024-01-01_2025-05-14.json"
    assert comparison_path.name == (
        "intrabar_comparison_AAA-BBB_2024-01-01_2025-05-14.md"
    )
    assert comparison.conflicts > 0
    assert comparison.take_profit_first > 0
    assert comparison.fallbacks == 0
    assert comparison.sessions_requested == comparison.by_resolution.get(
        "INTRADAY_PATH", 0
    ) + comparison.by_resolution.get("SAME_MINUTE", 0)
    assert comparison.api is None
    assert comparison.changed_trades > 0
    assert comparison.intrabar.exits_by_reason.get("TAKE_PROFIT", 0) > (
        comparison.v1.exits_by_reason.get("TAKE_PROFIT", 0)
    )
    assert comparison.intrabar_report_checksum == outcome.report_checksum
    assert comparison.v1_report_checksum != outcome.report_checksum
    assert comparison.intrabar_run_id == outcome.run_id

    with Session(engine) as session:
        stored = session.query(BacktestRunModel).all()

    # Sólo se guarda la ejecución intrabarra; la v1 es la referencia en memoria.
    assert [run.run_id for run in stored] == [outcome.run_id]
    assert "intrabar_resolution" in stored[0].parameters

    markdown = comparison_path.read_text(encoding="utf-8")
    assert markdown == intrabar_comparison_to_markdown(comparison)
    assert "| Sesiones con conflicto SL / TP |" in markdown
    assert "| Retorno acumulado |" in markdown
    assert "Sin proveedor Alpaca." in markdown

    summary = cli.format_intrabar_comparison(comparison, comparison_path)
    assert "Comparación v1 (SL primero) → intrabarra" in summary
