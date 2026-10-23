from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.cli.ingest import (
    IngestionStatus,
    format_results,
    parse_args,
    resolve_symbols,
    run_ingestion,
)
from algo_trading.config.settings import ApplicationSettings
from algo_trading.data.providers.yfinance import (
    YahooCorporateEvent,
    YahooDailyRow,
    YahooHistory,
    YFinanceProvider,
)
from algo_trading.persistence.models import Base
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted
from algo_trading.persistence.models.data_snapshot import DataSnapshotModel
from algo_trading.persistence.models.market_data import MarketBarRaw

SETTINGS = ApplicationSettings.from_yaml()

SESSIONS = (
    date(2020, 8, 27),
    date(2020, 8, 28),
    date(2020, 8, 31),
    date(2020, 9, 1),
    date(2020, 9, 2),
)
YAHOO_CLOSES = ("125", "124.8", "129.04", "134.18", "131.4")


class FakeYahoo:
    """Simula Yahoo: devuelve sólo las filas dentro de la ventana pedida."""

    def __init__(self, failing: frozenset[str] = frozenset()) -> None:
        self.failing = failing

    def __call__(self, symbol: str, start: date, end: date) -> YahooHistory:
        if symbol in self.failing:
            raise ConnectionError(f"sin respuesta para {symbol}")

        rows = tuple(
            YahooDailyRow(
                session_date=session,
                open=Decimal(close),
                high=Decimal(close) + 1,
                low=Decimal(close) - 1,
                close=Decimal(close),
                volume=Decimal("4000"),
            )
            for session, close in zip(SESSIONS, YAHOO_CLOSES, strict=True)
            if start <= session <= end
        )
        splits = (
            (YahooCorporateEvent(ex_date=date(2020, 8, 31), value=Decimal("4")),)
            if symbol == "AAPL"
            else ()
        )
        return YahooHistory(rows=rows, splits=splits, dividends=())


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def provider(failing: frozenset[str] = frozenset()) -> YFinanceProvider:
    return YFinanceProvider(
        fetcher=FakeYahoo(failing),
        clock=lambda: datetime(2026, 10, 22, tzinfo=UTC),
    )


def count(engine: Engine, model: type[Base], symbol: str) -> int:
    with Session(engine) as session:
        column = model.symbol  # type: ignore[attr-defined]
        return (
            session.scalar(
                select(func.count()).select_from(model).where(column == symbol)
            )
            or 0
        )


def test_ingests_raw_and_adjusted_bars_per_symbol(engine: Engine) -> None:
    results = run_ingestion(
        symbols=["AAPL", "MSFT"],
        start=date(2020, 8, 27),
        end=date(2020, 9, 2),
        provider=provider(),
        session_factory=sessionmaker(bind=engine),
    )

    assert [(r.symbol, r.status, r.bars, r.adjusted_bars) for r in results] == [
        ("AAPL", IngestionStatus.INGESTED, 5, 5),
        ("MSFT", IngestionStatus.INGESTED, 5, 5),
    ]
    assert results[0].splits == 1
    assert results[0].snapshot_id == "yfinance:1d:AAPL:2020-08-27:2020-09-02"

    with Session(engine) as session:
        raw_close = session.scalar(
            select(MarketBarRaw.close).where(
                MarketBarRaw.symbol == "AAPL",
                MarketBarRaw.timestamp == datetime(2020, 8, 27),
            )
        )
        adjusted_closes = session.scalars(
            select(MarketBarAdjusted.close)
            .where(MarketBarAdjusted.symbol == "AAPL")
            .order_by(MarketBarAdjusted.timestamp)
        ).all()

    # Raw reconstruido (x4) y ajustado de vuelta a los precios de Yahoo.
    assert raw_close == Decimal("500")
    assert list(adjusted_closes) == [Decimal(close) for close in YAHOO_CLOSES]


def test_second_run_is_up_to_date_without_duplicates(engine: Engine) -> None:
    factory = sessionmaker(bind=engine)
    arguments = {
        "symbols": ["AAPL"],
        "start": date(2020, 8, 27),
        "end": date(2020, 9, 2),
        "session_factory": factory,
    }
    run_ingestion(provider=provider(), **arguments)  # type: ignore[arg-type]

    results = run_ingestion(provider=provider(), **arguments)  # type: ignore[arg-type]

    assert results[0].status is IngestionStatus.UP_TO_DATE
    assert results[0].window_start == date(2020, 9, 3)
    assert results[0].adjusted_bars == 5
    assert count(engine, MarketBarRaw, "AAPL") == 5
    assert count(engine, MarketBarAdjusted, "AAPL") == 5


def test_resumes_after_last_stored_bar(engine: Engine) -> None:
    factory = sessionmaker(bind=engine)
    run_ingestion(
        symbols=["AAPL"],
        start=date(2020, 8, 27),
        end=date(2020, 8, 28),
        provider=provider(),
        session_factory=factory,
    )

    results = run_ingestion(
        symbols=["AAPL"],
        start=date(2020, 8, 27),
        end=date(2020, 9, 2),
        provider=provider(),
        session_factory=factory,
    )

    assert results[0].status is IngestionStatus.INGESTED
    assert results[0].window_start == date(2020, 8, 29)
    assert results[0].bars == 3
    assert count(engine, MarketBarRaw, "AAPL") == 5

    with Session(engine) as session:
        snapshots = session.scalar(select(func.count()).select_from(DataSnapshotModel))
        first_adjusted = session.scalar(
            select(MarketBarAdjusted.close)
            .where(MarketBarAdjusted.symbol == "AAPL")
            .order_by(MarketBarAdjusted.timestamp)
        )

    assert snapshots == 2
    assert count(engine, MarketBarAdjusted, "AAPL") == 5
    assert first_adjusted == Decimal("125")


def test_failure_in_one_symbol_does_not_stop_the_rest(engine: Engine) -> None:
    results = run_ingestion(
        symbols=["AAPL", "MSFT"],
        start=date(2020, 8, 27),
        end=date(2020, 9, 2),
        provider=provider(failing=frozenset({"AAPL"})),
        session_factory=sessionmaker(bind=engine),
    )

    assert results[0].status is IngestionStatus.FAILED
    assert results[0].error is not None
    assert "ConnectionError" in results[0].error
    assert results[1].status is IngestionStatus.INGESTED
    assert count(engine, MarketBarRaw, "AAPL") == 0
    assert count(engine, MarketBarRaw, "MSFT") == 5
    assert "FAILED" in format_results(results, provider())


def test_rejects_end_before_start(engine: Engine) -> None:
    with pytest.raises(ValueError, match="end"):
        run_ingestion(
            symbols=["AAPL"],
            start=date(2020, 9, 2),
            end=date(2020, 8, 27),
            provider=provider(),
            session_factory=sessionmaker(bind=engine),
        )


def test_parse_args_defaults_to_initial_universe() -> None:
    arguments = parse_args([])

    assert arguments.symbols is None
    assert arguments.universe is None
    assert resolve_symbols(arguments, SETTINGS) == (
        "AAPL",
        "MSFT",
        "GOOGL",
        "SPY",
        "QQQ",
    )
    assert arguments.start == date(2020, 1, 1)
    assert arguments.end is None
    assert arguments.adjustment_version == "splits-v1"


def test_parse_args_accepts_custom_window() -> None:
    arguments = parse_args(
        ["--symbols", "aapl", "--start", "2021-01-04", "--end", "2021-12-31"]
    )

    assert arguments.symbols == ["aapl"]
    assert arguments.start == date(2021, 1, 4)
    assert arguments.end == date(2021, 12, 31)


def test_resolve_symbols_from_diversified_universe() -> None:
    symbols = resolve_symbols(parse_args(["--universe", "Diversified"]), SETTINGS)

    assert len(symbols) == 15
    assert symbols[:5] == ("AAPL", "MSFT", "GOOGL", "QQQ", "SPY")
    assert {"XOM", "JPM", "JNJ", "PG", "CAT", "GLD", "TLT", "XLE", "XLF", "XLV"} <= (
        set(symbols)
    )


def test_explicit_symbols_take_precedence_and_are_uppercased() -> None:
    assert resolve_symbols(parse_args(["--symbols", "aapl", "tlt"]), SETTINGS) == (
        "AAPL",
        "TLT",
    )


def test_symbols_and_universe_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--symbols", "AAPL", "--universe", "initial"])


def test_unknown_universe_lists_available() -> None:
    with pytest.raises(ValueError, match="diversified, initial"):
        resolve_symbols(parse_args(["--universe", "global"]), SETTINGS)
