import random
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.analytics.walk_forward import (
    WalkForwardAnalyzer,
    WalkForwardConfig,
    WalkForwardReport,
    walk_forward_to_markdown,
)
from algo_trading.backtesting import BacktestEngine, BacktestParameters
from algo_trading.backtesting.run_service import BacktestRunRequest, BacktestRunService
from algo_trading.backtesting.sensitivity import (
    ParameterGrid,
    SensitivityAnalysisService,
)
from algo_trading.cli.backtest import (
    build_grid,
    build_request,
    format_walk_forward_summary,
    parse_args,
    run_walk_forward_command,
)
from algo_trading.config.settings import ApplicationSettings, SignalSettings
from algo_trading.data.market_bar import MarketBar
from algo_trading.persistence.models import Base
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted
from algo_trading.signals import (
    CompositeSignalGenerator,
    MomentumBreakoutGenerator,
    SectorRotationFilter,
    SignalContext,
)

START = datetime(2022, 1, 1, tzinfo=UTC)
DAYS = 730
SECTOR_FILTER = SectorRotationFilter(
    sector_map={"A": "technology", "B": "energy"},
    sector_etfs={"technology": "XLK", "energy": "XLE"},
    lookback=20,
    top_k=1,
    allow_unmapped=True,
)
GRID = ParameterGrid(
    stop_loss_atr_multiples=(Decimal("1"), Decimal("2")),
    max_position_exposures=(Decimal("0.20"),),
    use_sector_filter=(False, True),
)
CONFIG = WalkForwardConfig(
    in_sample_months=6, out_of_sample_months=3, min_trades=1, grid=GRID
)
PARAMETERS = BacktestParameters(initial_cash=Decimal("100000"), cost_profile="base")


def random_walk(symbol: str, seed: int, drift: int = 0) -> list[MarketBar]:
    generator = random.Random(seed)
    close = Decimal("100")
    bars = []

    for day in range(DAYS):
        open_price = max(
            Decimal("5"), close + Decimal(generator.randint(-60, 80)) / 100
        )
        close = max(
            Decimal("5"),
            open_price + Decimal(generator.randint(-150, 180 + drift)) / 100,
        )
        bars.append(
            MarketBar(
                symbol=symbol,
                timestamp=START + timedelta(days=day),
                timeframe="1d",
                source="test",
                open=open_price,
                high=max(open_price, close) + Decimal("0.5"),
                low=max(Decimal("1"), min(open_price, close) - Decimal("0.5")),
                close=close,
                volume=Decimal("1000"),
            )
        )

    return bars


def synthetic_bars() -> dict[str, list[MarketBar]]:
    return {symbol: random_walk(symbol, seed) for seed, symbol in enumerate("ABC")}


def reference_bars() -> dict[str, list[MarketBar]]:
    # El liderazgo sectorial alterna: XLK y XLE tienen deriva cíclica opuesta.
    return {
        "SPY": random_walk("SPY", 10),
        "XLK": random_walk("XLK", 11, drift=20),
        "XLE": random_walk("XLE", 12, drift=-20),
    }


def analyzer() -> WalkForwardAnalyzer:
    return WalkForwardAnalyzer(
        synthetic_bars(),
        reference_bars=reference_bars(),
        sector_filter=SECTOR_FILTER,
    )


@pytest.fixture(scope="module")
def report() -> WalkForwardReport:
    return analyzer().run(PARAMETERS, CONFIG)


def test_grid_defaults_keep_v1_combinations() -> None:
    grid = ParameterGrid()

    assert grid.use_sector_filter == (False,)
    assert not grid.includes_sector_filter
    assert all(not item.use_sector_filter for item in grid.combinations())


def test_sector_filter_is_the_innermost_grid_axis() -> None:
    combinations = GRID.combinations()

    assert [(c.stop_loss_atr_multiple, c.use_sector_filter) for c in combinations] == [
        (Decimal("1"), False),
        (Decimal("1"), True),
        (Decimal("2"), False),
        (Decimal("2"), True),
    ]
    assert GRID.includes_sector_filter

    with pytest.raises(ValueError, match="use_sector_filter"):
        ParameterGrid(use_sector_filter=(True, True))


def test_sweep_rejects_sector_filter_axis() -> None:
    service = SensitivityAnalysisService(BacktestRunService(session=None))  # type: ignore[arg-type]
    request = BacktestRunRequest(
        symbols=("A",),
        start=date(2022, 1, 1),
        end=date(2022, 12, 31),
        parameters=PARAMETERS,
    )

    with pytest.raises(ValueError, match="walk-forward"):
        service.run(request, GRID, persist=False)


def test_grid_with_filter_requires_sector_filter() -> None:
    with pytest.raises(ValueError, match="sector_filter"):
        WalkForwardAnalyzer(synthetic_bars()).run(PARAMETERS, CONFIG)


def test_filtered_signals_are_computed_once_and_invariant_to_future_bars() -> None:
    wf = analyzer()
    window_end = date(2022, 12, 31)
    cached = wf.signals(PARAMETERS.signal, use_sector_filter=True)

    assert wf.signals(PARAMETERS.signal, use_sector_filter=True) is cached

    generator = CompositeSignalGenerator(
        MomentumBreakoutGenerator(PARAMETERS.signal), filters=[SECTOR_FILTER]
    )
    truncated_context = SignalContext(
        {
            symbol: [bar for bar in bars if bar.timestamp.date() <= window_end]
            for symbol, bars in reference_bars().items()
        }
    )
    base = wf.signals(PARAMETERS.signal)

    for symbol, bars in synthetic_bars().items():
        truncated = [bar for bar in bars if bar.timestamp.date() <= window_end]
        fresh = generator.generate(truncated, truncated_context)
        in_window = [
            signal
            for signal in cached[symbol]
            if signal.signal_timestamp.date() <= window_end
        ]

        assert [s.signal_timestamp for s in in_window] == [
            s.signal_timestamp for s in fresh
        ]
        assert set(cached[symbol]) <= set(base[symbol])

    assert sum(len(s) for s in cached.values()) < sum(len(s) for s in base.values())


def test_filtered_evaluation_matches_engine_with_generator() -> None:
    wf = analyzer()
    window_start = date(2022, 7, 1)
    window_end = date(2022, 12, 31)
    sliced = {
        symbol: [bar for bar in bars if bar.timestamp.date() <= window_end]
        for symbol, bars in synthetic_bars().items()
    }

    cached = wf.evaluate(PARAMETERS, window_start, window_end, use_sector_filter=True)
    fresh = BacktestEngine(PARAMETERS).run(
        sliced,
        trading_start=window_start,
        signal_generator=CompositeSignalGenerator(
            MomentumBreakoutGenerator(), filters=[SECTOR_FILTER]
        ),
        signal_context=SignalContext(reference_bars()),
    )

    assert cached[0].use_sector_filter
    assert cached[1] == fresh.trades
    assert cached[2] == fresh.equity_curve


def test_in_sample_grid_evaluates_both_filter_values(report: WalkForwardReport) -> None:
    for result in report.windows:
        assert [e.use_sector_filter for e in result.in_sample] == [
            False,
            True,
            False,
            True,
        ]
        assert result.out_of_sample.use_sector_filter is (
            result.selected.use_sector_filter
        )

    assert report.sector_filter == SECTOR_FILTER
    assert report.sector_filter_blocked_signals
    assert report.sector_filter_selections == sum(
        result.selected.use_sector_filter for result in report.windows
    )


def test_filter_study_matches_walk_forward_restricted_to_each_value(
    report: WalkForwardReport,
) -> None:
    assert [study.use_sector_filter for study in report.sector_filter_study] == [
        False,
        True,
    ]

    for study in report.sector_filter_study:
        restricted = CONFIG.model_copy(
            update={
                "grid": GRID.model_copy(
                    update={"use_sector_filter": (study.use_sector_filter,)}
                )
            }
        )
        direct = analyzer().run(PARAMETERS, restricted)

        assert direct.sector_filter_study == ()
        assert all(
            selected.use_sector_filter is study.use_sector_filter
            for selected in study.selected
        )
        assert direct.out_of_sample == study.out_of_sample
        assert direct.walk_forward_efficiency == study.walk_forward_efficiency
        assert tuple(r.out_of_sample for r in direct.windows) == (
            study.out_of_sample_by_window
        )


def test_without_filter_axis_report_has_no_filter_study() -> None:
    grid = GRID.model_copy(update={"use_sector_filter": (False,)})
    plain = WalkForwardAnalyzer(synthetic_bars()).run(
        PARAMETERS, CONFIG.model_copy(update={"grid": grid})
    )

    assert plain.sector_filter is None
    assert plain.sector_filter_blocked_signals is None
    assert plain.sector_filter_study == ()
    assert "## Filtro de fuerza relativa sectorial" not in walk_forward_to_markdown(
        plain
    )


def test_markdown_compares_filter_variants(report: WalkForwardReport) -> None:
    markdown = walk_forward_to_markdown(report)

    assert "## Filtro de fuerza relativa sectorial" in markdown
    assert (
        f"La selección IS eligió el filtro en **{report.sector_filter_selections} de "
        f"{len(report.windows)}** ventanas"
    ) in markdown
    assert "| Sin filtro (señal v1) |" in markdown
    assert "| Con filtro sectorial |" in markdown
    assert "| **Optimizado (IS elige)** |" in markdown
    assert "### Por ventana" in markdown
    assert "filtro sectorial no, sí" in markdown


# --- CLI ---------------------------------------------------------------------


def adjusted_rows(bars: list[MarketBar]) -> list[MarketBarAdjusted]:
    return [
        MarketBarAdjusted(
            symbol=bar.symbol,
            timestamp=bar.timestamp.replace(tzinfo=None),
            timeframe="1d",
            source="yfinance",
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            adjustment_factor=Decimal("1"),
            adjustment_version="splits-v1",
        )
        for bar in bars
    ]


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)

    with Session(database) as session:
        for bars in [*synthetic_bars().values(), *reference_bars().values()]:
            session.add_all(adjusted_rows(bars))
        session.commit()

    yield database
    database.dispose()


@pytest.fixture
def settings() -> ApplicationSettings:
    local = ApplicationSettings.from_yaml()
    return local.model_copy(
        update={
            "universes": {**local.universes, "test_refs": ("SPY", "XLK", "XLE")},
            "signals": SignalSettings(
                reference_universe="test_refs",
                relative_strength_lookback=20,
                sector_top_k=1,
                sector_etfs={"technology": "XLK", "energy": "XLE"},
                sector_map={"A": "technology", "B": "energy"},
            ),
        }
    )


WF_ARGS = [
    "--walk-forward",
    "--symbols",
    "A",
    "B",
    "C",
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
    "1",
    "2",
    "--exposures",
    "0.20",
    "--max-positions",
    "5",
    "--risk-per-trade",
    "0.005",
]


def test_sector_filter_grid_option(settings: ApplicationSettings) -> None:
    default = parse_args(WF_ARGS)
    with_filter = parse_args([*WF_ARGS, "--sector-filter-grid", "off", "on"])

    assert build_grid(default, build_request(default, settings)).use_sector_filter == (
        False,
    )
    assert build_request(default, settings).reference_symbols == ()
    request = build_request(with_filter, settings)
    assert build_grid(with_filter, request).use_sector_filter == (False, True)
    assert request.reference_symbols == ("SPY", "XLK", "XLE")

    with pytest.raises(SystemExit):
        parse_args(["--sector-filter-grid", "off", "on"])


def test_walk_forward_command_loads_reference_data_once(
    engine: Engine,
    settings: ApplicationSettings,
    tmp_path: Path,
) -> None:
    sessions_opened = 0
    factory = sessionmaker(bind=engine)

    def counting_factory() -> Session:
        nonlocal sessions_opened
        sessions_opened += 1
        return factory()

    arguments = parse_args(
        [
            *WF_ARGS,
            "--sector-filter-grid",
            "off",
            "on",
            "--report-tag",
            "sector_filter",
            "--output-dir",
            str(tmp_path),
        ]
    )

    wf_report, json_path, markdown_path = run_walk_forward_command(
        arguments,
        session_factory=counting_factory,
        settings=settings,
        calendar=None,
    )

    assert sessions_opened == 1
    assert json_path.name.startswith("walk_forward_sector_filter_A-B-C_")
    assert wf_report.config.grid.use_sector_filter == (False, True)
    assert wf_report.sector_filter == SECTOR_FILTER.model_copy(
        update={"benchmark": "SPY"}
    )
    assert len(wf_report.sector_filter_study) == 2
    assert "## Filtro de fuerza relativa sectorial" in markdown_path.read_text(
        encoding="utf-8"
    )
    summary = format_walk_forward_summary(wf_report, json_path, markdown_path)
    assert "Filtro sectorial elegido en" in summary
    assert "con filtro: retorno OOS" in summary
