import random
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.cli.backtest import (
    format_walk_forward_summary,
    parse_args,
    run_walk_forward_command,
)
from algo_trading.config.settings import ApplicationSettings
from algo_trading.persistence.models import BacktestRunModel, Base
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted

START = datetime(2022, 1, 1)


def adjusted_rows(symbol: str, seed: int, days: int) -> list[MarketBarAdjusted]:
    generator = random.Random(seed)
    close = Decimal("100")
    rows = []

    for day in range(days):
        open_price = max(
            Decimal("5"), close + Decimal(generator.randint(-60, 80)) / 100
        )
        close = max(
            Decimal("5"), open_price + Decimal(generator.randint(-150, 180)) / 100
        )
        rows.append(
            MarketBarAdjusted(
                symbol=symbol,
                timestamp=START + timedelta(days=day),
                timeframe="1d",
                source="yfinance",
                open=open_price,
                high=max(open_price, close) + Decimal("0.5"),
                low=max(Decimal("1"), min(open_price, close) - Decimal("0.5")),
                close=close,
                volume=Decimal("1000"),
                adjustment_factor=Decimal("1"),
                adjustment_version="splits-v1",
            )
        )

    return rows


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)

    with Session(database) as session:
        session.add_all(adjusted_rows("AAA", 1, 730))
        session.add_all(adjusted_rows("BBB", 2, 730))
        session.commit()

    yield database
    database.dispose()


def test_walk_forward_defaults() -> None:
    arguments = parse_args([])

    assert not arguments.walk_forward
    assert arguments.is_months == 36
    assert arguments.oos_months == 12
    assert arguments.wf_step_months is None
    assert arguments.wf_metric == "profit_factor"
    assert arguments.min_trades == 10


def test_walk_forward_command_writes_report_without_persisting(
    engine: Engine,
    tmp_path: Path,
) -> None:
    arguments = parse_args(
        [
            "--walk-forward",
            "--symbols",
            "AAA",
            "BBB",
            "--start",
            "2022-01-01",
            "--end",
            "2023-12-31",
            "--is-months",
            "6",
            "--oos-months",
            "3",
            "--wf-metric",
            "sharpe",
            "--min-trades",
            "1",
            "--sl-multiples",
            "1",
            "2",
            "--exposures",
            "0.10",
            "--output-dir",
            str(tmp_path),
        ]
    )

    report, json_path, markdown_path = run_walk_forward_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        settings=ApplicationSettings.from_yaml(),
        calendar=None,
    )

    assert len(report.windows) == 6
    assert report.config.selection_metric == "sharpe"
    assert all(len(result.in_sample) == 2 for result in report.windows)
    assert json_path.name == (
        "walk_forward_AAA-BBB_2022-01-01_2023-12-31_base_is6-oos3_sharpe.json"
    )
    assert markdown_path.name.startswith("walk_forward_")
    assert markdown_path.read_text(encoding="utf-8").startswith(
        "# Walk-Forward Optimization"
    )
    summary = format_walk_forward_summary(report, json_path, markdown_path)
    assert "WFE:" in summary
    assert "OOS encadenado" in summary

    with Session(engine) as session:
        assert session.query(BacktestRunModel).count() == 0


def test_portfolio_scaling_options_and_report_tag(
    engine: Engine,
    tmp_path: Path,
) -> None:
    arguments = parse_args(
        [
            "--walk-forward",
            "--symbols",
            "AAA",
            "BBB",
            "--start",
            "2022-01-01",
            "--end",
            "2023-12-31",
            "--is-months",
            "6",
            "--oos-months",
            "3",
            "--min-trades",
            "1",
            "--sl-multiples",
            "1.5",
            "--tp-multiples",
            "3",
            "--exposures",
            "0.10",
            "--positions-grid",
            "2",
            "5",
            "--risk-grid",
            "0.005",
            "0.01",
            "--report-tag",
            "portfolio_scaling",
            "--output-dir",
            str(tmp_path),
        ]
    )

    report, json_path, markdown_path = run_walk_forward_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        settings=ApplicationSettings.from_yaml(),
        calendar=None,
    )

    assert len(report.config.grid.combinations()) == 4
    assert len(report.scaling_study) == 4
    assert json_path.name.startswith("walk_forward_portfolio_scaling_AAA-BBB_")
    assert "## Escalado de cartera" in markdown_path.read_text(encoding="utf-8")
    assert "Escalado de cartera" in format_walk_forward_summary(
        report, json_path, markdown_path
    )


def test_grid_defaults_to_base_portfolio_scale() -> None:
    arguments = parse_args(["--max-positions", "8", "--risk-per-trade", "0.01"])

    assert arguments.positions_grid is None
    assert arguments.risk_grid is None
    assert arguments.max_positions == 8
    assert arguments.risk_per_trade == Decimal("0.01")
