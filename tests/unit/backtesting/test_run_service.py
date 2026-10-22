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
    parse_args,
    run_backtest_command,
)
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
        ]
    )

    outcome, json_path, markdown_path = run_backtest_command(
        arguments,
        session_factory=sessionmaker(bind=engine),
        default_initial_cash=Decimal("50000"),
        max_positions=5,
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
        default_initial_cash=Decimal("50000"),
        max_positions=5,
        calendar=None,
    )

    assert outcome.run_id is None
    assert outcome.report.metrics.dividend_income == 0

    with Session(engine) as session:
        assert count(session, BacktestRunModel) == 0


def test_cli_defaults() -> None:
    arguments = parse_args([])

    assert arguments.symbols == ["AAPL", "MSFT", "GOOGL", "SPY", "QQQ"]
    assert arguments.start == date(2020, 1, 1)
    assert arguments.end is None
    assert arguments.initial_cash is None
    assert arguments.gap_threshold_atr == Decimal("0.5")
    assert arguments.adjustment_version == "splits-v1"
    assert arguments.calendar == "NYSE"
    assert not arguments.no_persist
    assert not arguments.no_dividends
    assert arguments.list_runs is None


def test_format_runs_without_runs() -> None:
    assert format_runs(()) == "No hay ejecuciones guardadas."
