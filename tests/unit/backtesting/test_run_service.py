import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.analytics import report_to_json
from algo_trading.backtesting import BacktestParameters
from algo_trading.backtesting.run_service import (
    BacktestRunRequest,
    BacktestRunService,
    MissingMarketDataError,
    default_report_name,
)
from algo_trading.cli.backtest import (
    format_runs,
    format_summary,
    format_sweep_summary,
    parse_args,
    run_backtest_command,
    run_sweep_command,
)
from algo_trading.config.settings import ApplicationSettings
from algo_trading.data.market_calendar import SessionCoverageError, TradingSession
from algo_trading.execution import ExitReason
from algo_trading.persistence.models import (
    BacktestEquitySnapshotModel,
    BacktestRunModel,
    Base,
    CorporateAction,
    CorporateActionType,
)
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted
from algo_trading.persistence.repositories.backtest_run_repository import (
    BacktestRunRepository,
)

START = datetime(2024, 1, 1)
SETTINGS = ApplicationSettings.from_yaml()
CREATED_AT = datetime(2026, 10, 22, 21, 0, tzinfo=UTC)


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


def take_profit_rows() -> list[MarketBarAdjusted]:
    """Señal en el índice 20, entrada en 21 y take profit en 22."""
    rows = []

    for index in range(20):
        close = Decimal("100") + Decimal("0.1") * index
        rows.append(
            adjusted_row(
                index, (str(close), str(close + 1), str(close - 1), str(close))
            )
        )

    rows.extend(
        [
            adjusted_row(20, ("110", "111", "109", "110")),
            adjusted_row(21, ("110", "110.5", "109.5", "110")),
            adjusted_row(22, ("111", "115", "110.5", "114")),
        ]
    )
    return rows


def corporate_action(
    index: int,
    action_type: CorporateActionType,
    value: str,
) -> CorporateAction:
    is_split = action_type is CorporateActionType.SPLIT
    return CorporateAction(
        symbol="AAA",
        action_type=action_type,
        ex_date=(START + timedelta(days=index)).date(),
        split_ratio=Decimal(value) if is_split else None,
        dividend_amount=None if is_split else Decimal(value),
        source="yfinance",
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)

    with Session(database) as session:
        session.add_all(take_profit_rows())
        # Otra versión de ajuste y otra fuente: no deben mezclarse.
        session.add(adjusted_row(0, ("1", "1", "1", "1"), adjustment_version="v0"))
        session.add(adjusted_row(0, ("1", "1", "1", "1"), source="other"))
        session.add_all(
            [
                # Dividendo raw 1.0 antes de un split 2:1 → 0.5 ajustado.
                corporate_action(10, CorporateActionType.DIVIDEND, "1.0"),
                corporate_action(15, CorporateActionType.SPLIT, "2"),
                corporate_action(18, CorporateActionType.DIVIDEND, "0.3"),
                # Fuera del periodo.
                corporate_action(40, CorporateActionType.DIVIDEND, "9"),
            ]
        )
        session.commit()

    yield database
    database.dispose()


class FakeCalendar:
    def __init__(self, missing: frozenset[date] = frozenset()) -> None:
        self.missing = missing

    @property
    def name(self) -> str:
        return "FAKE"

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        days = (end - start).days + 1
        dates = [start + timedelta(days=offset) for offset in range(days)]
        return tuple(
            TradingSession(
                session_date=value,
                market_open=datetime(
                    value.year, value.month, value.day, 14, 30, tzinfo=UTC
                ),
                market_close=datetime(
                    value.year, value.month, value.day, 21, tzinfo=UTC
                ),
                is_early_close=False,
            )
            for value in dates
            if value not in self.missing
        )


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


def service(
    session: Session,
    calendar: FakeCalendar | None = None,
    run_id: str = "run-1",
) -> BacktestRunService:
    return BacktestRunService(
        session,
        calendar=calendar,
        clock=lambda: CREATED_AT,
        id_factory=lambda: run_id,
    )


def count(session: Session, model: type[Base]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def test_loads_adjusted_bars_for_source_and_version_with_utc_timestamps(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        bars = service(session).load_bars(request())

    assert list(bars) == ["AAA"]
    assert len(bars["AAA"]) == 23
    assert bars["AAA"][0].close == Decimal("100")
    assert bars["AAA"][0].timestamp == datetime(2024, 1, 1, tzinfo=UTC)


def test_end_date_is_inclusive(engine: Engine) -> None:
    with Session(engine) as session:
        bars = service(session).load_bars(request(end=date(2024, 1, 22)))
        outcome = service(session).run(request(end=date(2024, 1, 22)), persist=False)

    assert bars["AAA"][-1].timestamp == datetime(2024, 1, 22, tzinfo=UTC)
    # Sin la barra del take profit, la posición se cierra por fin de datos.
    assert outcome.report.trades[0].exit_reason is ExitReason.END_OF_DATA


def test_dividends_are_adjusted_by_later_splits(engine: Engine) -> None:
    with Session(engine) as session:
        dividends = service(session).load_adjusted_dividends(request())

    assert [(d.ex_date, d.amount) for d in dividends["AAA"]] == [
        (date(2024, 1, 11), Decimal("0.50")),
        (date(2024, 1, 19), Decimal("0.3")),
    ]


def test_runs_backtest_and_persists_run_trades_and_equity(engine: Engine) -> None:
    with Session(engine) as session:
        outcome = service(session).run(request())

        assert outcome.run_id == "run-1"
        assert (
            outcome.report_checksum
            == hashlib.sha256(
                report_to_json(outcome.report).encode("utf-8")
            ).hexdigest()
        )

        repository = BacktestRunRepository(session)
        summary = repository.get_summary("run-1")
        trades = repository.get_trades("run-1")
        curve = repository.get_equity_curve("run-1")

    assert summary is not None
    assert summary.created_at == CREATED_AT
    assert summary.symbols == ("AAA",)
    assert summary.start_date == date(2024, 1, 1)
    assert summary.end_date == date(2024, 1, 23)
    assert summary.total_trades == 1
    assert summary.parameters["gap_threshold_atr"] == "0.5"
    assert summary.final_equity == outcome.report.metrics.final_equity.quantize(
        Decimal("0.00000001")
    )
    assert summary.report_checksum == outcome.report_checksum
    assert [trade.exit_reason for trade in trades] == ["TAKE_PROFIT"]
    assert trades[0].net_pnl == outcome.report.trades[0].net_pnl.quantize(
        Decimal("0.00000001")
    )
    assert len(curve) == 23
    assert curve[0].timestamp == datetime(2024, 1, 1)


def test_no_persist_leaves_database_untouched(engine: Engine) -> None:
    with Session(engine) as session:
        outcome = service(session).run(request(), persist=False)

        assert outcome.run_id is None
        assert count(session, BacktestRunModel) == 0


def test_runs_can_be_compared_from_database(engine: Engine) -> None:
    with Session(engine) as session:
        service(session, run_id="run-a").run(request())
        later = BacktestRunService(
            session,
            clock=lambda: CREATED_AT + timedelta(hours=1),
            id_factory=lambda: "run-b",
        )
        later.run(request(end=date(2024, 1, 22)))

        runs = BacktestRunRepository(session).list_summaries()

    assert [run.run_id for run in runs] == ["run-b", "run-a"]
    assert runs[0].end_date == date(2024, 1, 22)
    assert "run-a" in format_runs(runs)


def test_calendar_is_applied_and_recorded(engine: Engine) -> None:
    with Session(engine) as session:
        outcome = service(session, calendar=FakeCalendar()).run(request())
        summary = BacktestRunRepository(session).get_summary("run-1")

    assert outcome.report.trading_calendar == "FAKE"
    assert summary is not None
    assert summary.trading_calendar == "FAKE"


def test_incomplete_series_against_calendar_fails_without_persisting(
    engine: Engine,
) -> None:
    calendar = FakeCalendar()
    # Con el calendario completo, falta una barra si borramos una del medio.
    with Session(engine) as session:
        session.delete(
            session.scalars(
                select(MarketBarAdjusted).where(
                    MarketBarAdjusted.timestamp == datetime(2024, 1, 10),
                    MarketBarAdjusted.adjustment_version == "splits-v1",
                    MarketBarAdjusted.source == "yfinance",
                )
            ).one()
        )
        session.commit()

        with pytest.raises(SessionCoverageError, match="2024-01-10"):
            service(session, calendar=calendar).run(request())

        assert count(session, BacktestRunModel) == 0
        assert count(session, BacktestEquitySnapshotModel) == 0


def test_missing_symbol_raises_with_symbol_list(engine: Engine) -> None:
    with (
        Session(engine) as session,
        pytest.raises(MissingMarketDataError, match="BBB, CCC"),
    ):
        service(session).run(request(symbols=("AAA", "BBB", "CCC")))


def test_request_rejects_inverted_period() -> None:
    with pytest.raises(ValueError, match="end"):
        request(end=date(2023, 12, 31))


def test_default_report_name_is_deterministic() -> None:
    assert (
        default_report_name(["MSFT", "AAPL"], date(2020, 1, 1), date(2026, 10, 21))
        == "backtest_AAPL-MSFT_2020-01-01_2026-10-21"
    )


def test_cli_writes_reports_and_persists_run(engine: Engine, tmp_path: Path) -> None:
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
            "--initial-cash",
            "50000",
        ]
    )

    outcome, json_path, markdown_path = run_backtest_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        settings=SETTINGS,
        calendar=None,
    )

    assert outcome.run_id is not None
    assert outcome.report.parameters.initial_cash == Decimal("50000")
    assert json_path == tmp_path / "backtest_AAA_2024-01-01_2024-01-23.json"
    assert markdown_path.read_text(encoding="utf-8").startswith("# Informe de backtest")
    summary = format_summary(outcome, json_path, markdown_path)
    assert outcome.run_id in summary
    assert "Trades: 1" in summary

    with Session(engine) as session:
        assert count(session, BacktestRunModel) == 1


def test_cli_no_persist_and_no_dividends(engine: Engine, tmp_path: Path) -> None:
    arguments = parse_args(
        [
            "--symbols",
            "AAA",
            "--start",
            "2024-01-01",
            "--end",
            "2024-01-23",
            "--output-dir",
            str(tmp_path),
            "--no-persist",
            "--no-dividends",
        ]
    )

    outcome, _, _ = run_backtest_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        settings=SETTINGS,
        calendar=None,
    )

    assert outcome.run_id is None
    assert outcome.report.metrics.dividend_income == 0

    with Session(engine) as session:
        assert count(session, BacktestRunModel) == 0


def test_cli_defaults() -> None:
    arguments = parse_args([])

    assert arguments.symbols is None
    assert arguments.universe is None
    assert arguments.take_profit_atr == Decimal("2")
    assert arguments.tp_multiples == [Decimal("2.0")]
    assert arguments.start == date(2020, 1, 1)
    assert arguments.end is None
    assert arguments.initial_cash is None
    assert arguments.gap_threshold_atr == Decimal("0.5")
    assert arguments.adjustment_version == "splits-v1"
    assert arguments.calendar == "NYSE"
    assert not arguments.no_persist
    assert not arguments.no_dividends
    assert arguments.list_runs is None
    assert arguments.cost_profile == "base"
    assert arguments.stop_loss_atr == Decimal("1")
    assert arguments.max_position_exposure == Decimal("0.10")
    assert not arguments.sweep
    assert arguments.sl_multiples == [Decimal("1.0"), Decimal("1.5"), Decimal("2.0")]
    assert arguments.exposures == [Decimal("0.10"), Decimal("0.15"), Decimal("0.20")]


def test_format_runs_without_runs() -> None:
    assert format_runs(()) == "No hay ejecuciones guardadas."


def cli_arguments(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--symbols",
        "AAA",
        "--start",
        "2024-01-01",
        "--end",
        "2024-01-23",
        "--output-dir",
        str(tmp_path),
        *extra,
    ]


@pytest.mark.parametrize(
    ("profile", "rate"),
    [
        ("optimista", Decimal("0.0002")),
        ("base", Decimal("0.0005")),
        ("Conservador", Decimal("0.0010")),
    ],
)
def test_cli_applies_cost_profile(
    engine: Engine,
    tmp_path: Path,
    profile: str,
    rate: Decimal,
) -> None:
    arguments = parse_args(
        cli_arguments(tmp_path, "--cost-profile", profile, "--no-persist")
    )

    outcome, _, markdown_path = run_backtest_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        settings=SETTINGS,
        calendar=None,
    )

    parameters = outcome.report.parameters
    assert parameters.cost_profile == profile.lower()
    assert parameters.costs.commission_rate == rate
    assert parameters.costs.slippage_rate == rate
    assert parameters.risk.commission_rate == rate
    assert f"| Perfil de costes | {profile.lower()} |" in markdown_path.read_text(
        encoding="utf-8"
    )


def test_more_expensive_profile_lowers_net_result(
    engine: Engine,
    tmp_path: Path,
) -> None:
    results = {}

    for profile in ("optimista", "conservador"):
        outcome, _, _ = run_backtest_command(
            parse_args(
                cli_arguments(tmp_path, "--cost-profile", profile, "--no-persist")
            ),
            session_factory=sessionmaker(bind=engine),
            settings=SETTINGS,
            calendar=None,
        )
        results[profile] = outcome.report.metrics

    assert results["conservador"].trades.total_costs > (
        results["optimista"].trades.total_costs
    )
    assert results["conservador"].final_equity < results["optimista"].final_equity


def test_cli_rejects_unknown_cost_profile(engine: Engine, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Perfil de costes desconocido"):
        run_backtest_command(
            parse_args(cli_arguments(tmp_path, "--cost-profile", "agresivo")),
            session_factory=sessionmaker(bind=engine),
            settings=SETTINGS,
            calendar=None,
        )


def test_cli_applies_stop_loss_multiple_and_exposure(
    engine: Engine,
    tmp_path: Path,
) -> None:
    arguments = parse_args(
        cli_arguments(
            tmp_path,
            "--stop-loss-atr",
            "1.5",
            "--max-position-exposure",
            "0.2",
            "--no-persist",
        )
    )

    outcome, _, _ = run_backtest_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        settings=SETTINGS,
        calendar=None,
    )

    trade = outcome.report.trades[0]
    assert outcome.report.parameters.risk.stop_loss_atr_multiple == Decimal("1.5")
    assert outcome.report.parameters.risk.max_position_exposure == Decimal("0.2")
    assert trade.stop_loss == trade.entry_executed_price - Decimal("1.5") * (
        trade.entry_atr
    )


def test_cli_sweep_runs_grid_and_persists_runs_with_shared_sweep_id(
    engine: Engine,
    tmp_path: Path,
) -> None:
    arguments = parse_args(
        cli_arguments(
            tmp_path,
            "--sweep",
            "--sl-multiples",
            "1",
            "2",
            "--exposures",
            "0.10",
            "0.20",
        )
    )

    report, json_path, markdown_path = run_sweep_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        settings=SETTINGS,
        calendar=None,
    )

    assert [
        (row.stop_loss_atr_multiple, row.max_position_exposure) for row in report.rows
    ] == [
        (Decimal("1"), Decimal("0.10")),
        (Decimal("1"), Decimal("0.20")),
        (Decimal("2"), Decimal("0.10")),
        (Decimal("2"), Decimal("0.20")),
    ]
    assert report.sweep_id is not None
    assert all(row.run_id is not None for row in report.rows)
    assert json_path.name == "sensitivity_AAA_2024-01-01_2024-01-23_base.json"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert markdown.startswith("# Análisis de sensibilidad")
    assert "| SL × ATR | TP × ATR | Exposición | Retorno | MDD | Win Rate" in (markdown)
    assert "Barrido: " in format_sweep_summary(report, json_path, markdown_path)

    with Session(engine) as session:
        runs = BacktestRunRepository(session).list_summaries(sweep_id=report.sweep_id)

    assert len(runs) == 4
    assert {run.run_id for run in runs} == {row.run_id for row in report.rows}
    assert {
        (
            run.parameters["risk"]["stop_loss_atr_multiple"],
            run.parameters["risk"]["max_position_exposure"],
        )
        for run in runs
    } == {("1", "0.10"), ("1", "0.20"), ("2", "0.10"), ("2", "0.20")}
    assert "1" in format_runs(runs)


def test_sweep_varies_results_with_parameters(engine: Engine, tmp_path: Path) -> None:
    report, _, _ = run_sweep_command(
        parse_args(cli_arguments(tmp_path, "--sweep", "--no-persist")),
        session_factory=sessionmaker(bind=engine),
        settings=SETTINGS,
        calendar=None,
    )

    assert len(report.rows) == 9
    assert report.sweep_id is None
    # Exposición mayor → posición mayor → mayor resultado absoluto (trade ganador).
    by_exposure = {
        row.max_position_exposure: row.final_equity
        for row in report.rows
        if row.stop_loss_atr_multiple == Decimal("1.0")
    }
    assert by_exposure[Decimal("0.20")] > by_exposure[Decimal("0.10")]


def test_sweep_includes_take_profit_axis(engine: Engine, tmp_path: Path) -> None:
    report, _, markdown_path = run_sweep_command(
        parse_args(
            cli_arguments(
                tmp_path,
                "--sweep",
                "--no-persist",
                "--sl-multiples",
                "1",
                "--tp-multiples",
                "2",
                "3",
                "--exposures",
                "0.10",
            )
        ),
        session_factory=sessionmaker(bind=engine),
        settings=SETTINGS,
        calendar=None,
    )

    assert [row.take_profit_atr_multiple for row in report.rows] == [
        Decimal("2"),
        Decimal("3"),
    ]
    # El escenario cierra en TP con 2 x ATR; con 3 x ATR no se alcanza (fin de datos).
    assert report.rows[0].cumulative_return != report.rows[1].cumulative_return
    assert "| 1 | 3 | 10.00 % |" in markdown_path.read_text(encoding="utf-8")


def test_cli_applies_take_profit_multiple(engine: Engine, tmp_path: Path) -> None:
    outcome, _, _ = run_backtest_command(
        parse_args(cli_arguments(tmp_path, "--take-profit-atr", "3", "--no-persist")),
        session_factory=sessionmaker(bind=engine),
        settings=SETTINGS,
        calendar=None,
    )

    trade = outcome.report.trades[0]
    assert outcome.report.parameters.risk.take_profit_atr_multiple == Decimal("3")
    assert trade.take_profit == trade.entry_executed_price + 3 * trade.entry_atr


def test_universe_label_is_used_in_report_names(
    engine: Engine,
    tmp_path: Path,
) -> None:
    settings = SETTINGS.model_copy(update={"universes": {"tiny": ("AAA",)}})

    _, json_path, _ = run_backtest_command(
        parse_args(
            [
                "--universe",
                "tiny",
                "--start",
                "2024-01-01",
                "--end",
                "2024-01-23",
                "--output-dir",
                str(tmp_path),
                "--no-persist",
            ]
        ),
        session_factory=sessionmaker(bind=engine),
        settings=settings,
        calendar=None,
    )

    assert json_path.name == "backtest_tiny_2024-01-01_2024-01-23.json"
