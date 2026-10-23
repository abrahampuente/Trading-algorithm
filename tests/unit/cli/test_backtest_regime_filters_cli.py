import random
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.analytics.report import BacktestReport
from algo_trading.cli.backtest import (
    build_request,
    build_signal_generator,
    format_summary,
    parse_args,
    run_backtest_command,
)
from algo_trading.config.settings import ApplicationSettings, SignalSettings
from algo_trading.persistence.models import BacktestRunModel, Base
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted
from algo_trading.signals import SectorRotationFilter, VixRegimeFilter

START = datetime(2022, 1, 1)
DAYS = 400
BASE_ARGS = ["--symbols", "AAA", "BBB", "--start", "2022-01-01", "--end", "2023-02-04"]


def adjusted_rows(
    symbol: str, seed: int, drift: int = 0, flat: str | None = None
) -> list[MarketBarAdjusted]:
    generator = random.Random(seed)
    close = Decimal("100")
    rows = []

    for day in range(DAYS):
        if flat is not None:
            open_price = close = Decimal(flat)
        else:
            open_price = max(
                Decimal("5"), close + Decimal(generator.randint(-60, 80)) / 100
            )
            close = max(
                Decimal("5"),
                open_price + Decimal(generator.randint(-150, 180 + drift)) / 100,
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
        session.add_all(adjusted_rows("AAA", 1))
        session.add_all(adjusted_rows("BBB", 2))
        session.add_all(adjusted_rows("SPY", 3))
        # XLK sube claramente más que XLE: tecnología es el sector más fuerte.
        session.add_all(adjusted_rows("XLK", 4, drift=60))
        session.add_all(adjusted_rows("XLE", 5, drift=-60))
        session.add_all(adjusted_rows("^VIX", 6, flat="30"))
        session.commit()

    yield database
    database.dispose()


@pytest.fixture
def settings() -> ApplicationSettings:
    local = ApplicationSettings.from_yaml()
    return local.model_copy(
        update={
            "universes": {
                **local.universes,
                "test_refs": ("SPY", "^VIX", "XLK", "XLE"),
            },
            "signals": SignalSettings(
                reference_universe="test_refs",
                relative_strength_lookback=20,
                sector_top_k=1,
                sector_etfs={"technology": "XLK", "energy": "XLE"},
                sector_map={"AAA": "technology", "BBB": "energy"},
            ),
        }
    )


def test_filters_are_disabled_by_default(settings: ApplicationSettings) -> None:
    arguments = parse_args(BASE_ARGS)

    assert not arguments.use_sector_relative_strength
    assert arguments.vix_filter_threshold is None
    assert build_signal_generator(arguments, settings) is None
    assert build_request(arguments, settings).reference_symbols == ()


def test_flags_build_filters_from_configuration(settings: ApplicationSettings) -> None:
    arguments = parse_args(
        [*BASE_ARGS, "--use-sector-relative-strength", "--vix-filter-threshold", "25"]
    )

    generator = build_signal_generator(arguments, settings)

    assert generator is not None
    assert generator.strategy_id == "momentum_breakout_v1+sector_rs+vix"
    sector_filter, vix_filter = generator.filters
    assert isinstance(sector_filter, SectorRotationFilter)
    assert sector_filter.lookback == 20
    assert sector_filter.top_k == 1
    assert sector_filter.allow_unmapped
    assert isinstance(vix_filter, VixRegimeFilter)
    assert vix_filter.max_level == Decimal("25")
    assert build_request(arguments, settings).reference_symbols == (
        "SPY",
        "^VIX",
        "XLK",
        "XLE",
    )


@pytest.mark.parametrize("mode", ["--sweep", "--walk-forward"])
def test_filters_are_rejected_in_sweep_and_walk_forward(mode: str) -> None:
    with pytest.raises(SystemExit):
        parse_args([mode, "--vix-filter-threshold", "25"])


def run(
    engine: Engine,
    settings: ApplicationSettings,
    tmp_path: Path,
    extra: list[str],
) -> tuple[Path, str]:
    arguments = parse_args([*BASE_ARGS, "--output-dir", str(tmp_path), *extra])
    outcome, json_path, markdown_path = run_backtest_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        settings=settings,
        calendar=None,
    )
    return json_path, format_summary(outcome, json_path, markdown_path)


def test_regime_filters_change_trades_and_are_persisted(
    engine: Engine,
    settings: ApplicationSettings,
    tmp_path: Path,
) -> None:
    v1_path, v1_summary = run(engine, settings, tmp_path, [])
    loose_path, _ = run(engine, settings, tmp_path, ["--vix-filter-threshold", "40"])
    strict_path, strict_summary = run(
        engine, settings, tmp_path, ["--vix-filter-threshold", "25"]
    )
    sector_path, sector_summary = run(
        engine, settings, tmp_path, ["--use-sector-relative-strength"]
    )

    v1_json = v1_path.read_text(encoding="utf-8")
    assert "signal_filters" not in v1_json
    assert "Filtros v2" not in v1_summary
    assert v1_path.name == "backtest_AAA-BBB_2022-01-01_2023-02-04.json"
    assert loose_path.name == "backtest_vix40_AAA-BBB_2022-01-01_2023-02-04.json"
    assert sector_path.name.startswith("backtest_sector-rs_AAA-BBB_")

    # VIX constante en 30: con umbral 40 no bloquea nada, con 25 todo.
    loose = loose_path.read_text(encoding="utf-8")
    assert '"filtered_signals": 0' in loose
    assert '"total_trades": 0' in strict_path.read_text(encoding="utf-8")
    assert "vix_regime" in strict_summary

    # Tecnología (AAA) es el único sector admitido: se bloquean las de BBB.
    assert "sector_rotation:SPY" in sector_summary
    v1_report = BacktestReport.model_validate_json(v1_json)
    sector_report = BacktestReport.model_validate_json(
        sector_path.read_text(encoding="utf-8")
    )
    assert {trade.symbol for trade in v1_report.trades} == {"AAA", "BBB"}
    assert {trade.symbol for trade in sector_report.trades} == {"AAA"}
    assert sector_report.signal_filters is not None
    assert sector_report.signal_filters.filtered_by == {
        "sector_rotation:SPY": sector_report.signal_filters.filtered_signals
    }

    with Session(engine) as session:
        runs = session.query(BacktestRunModel).all()
        stored = [run.parameters.get("signal_filters") for run in runs]

    assert len(runs) == 4
    assert stored.count(None) == 1
    assert {item["strategy_id"] for item in stored if item} == {
        "momentum_breakout_v1+vix",
        "momentum_breakout_v1+sector_rs",
    }
