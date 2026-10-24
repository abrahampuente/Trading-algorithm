import signal
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.cli import live
from algo_trading.config.settings import ApplicationSettings
from algo_trading.data.market_calendar import TradingSession
from algo_trading.execution.alpaca_broker import (
    LiveTradingNotAllowedError,
    RequestTiming,
)
from algo_trading.execution.broker import AccountSnapshot, BrokerPosition
from algo_trading.execution.daemon import ExecutionDaemon, MarketClock, MarketStatus
from algo_trading.execution.orders import OrderRequest, OrderState
from algo_trading.persistence.models import Base, MarketBarRaw

MONDAY = date(2026, 10, 26)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


SATURDAY_NOON = at(date(2026, 10, 24), 12)
REFRESHES: list[tuple[tuple[str, ...], date]] = []


class FakeCalendar:
    name = "TEST"

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        days = []
        day = start

        while day <= end:
            if day.weekday() < 5:
                days.append(
                    TradingSession(
                        session_date=day,
                        market_open=at(day, 13, 30),
                        market_close=at(day, 20),
                        is_early_close=False,
                    )
                )
            day += timedelta(days=1)

        return tuple(days)


class FakeBroker:
    def __init__(
        self,
        account_number: str = "PA123456",
        positions: Sequence[BrokerPosition] = (),
        blocked: bool = False,
    ) -> None:
        self.account_number = account_number
        self.positions = list(positions)
        self.blocked = blocked
        self.submitted: list[OrderRequest] = []
        self.log: list[RequestTiming] = []

    @property
    def request_log(self) -> tuple[RequestTiming, ...]:
        return tuple(self.log)

    def get_account(self) -> AccountSnapshot:
        return AccountSnapshot(
            account_number=self.account_number,
            status="ACTIVE",
            currency="USD",
            cash=Decimal("100000"),
            equity=Decimal("100000"),
            buying_power=Decimal("200000"),
            trading_blocked=self.blocked,
        )

    def get_positions(self) -> Sequence[BrokerPosition]:
        return tuple(self.positions)

    def submit_order(self, request: OrderRequest) -> OrderState:
        self.submitted.append(request)
        raise AssertionError("algo-live no debe enviar órdenes sin plan")

    def get_order(self, client_order_id: str) -> OrderState | None:
        return None

    def list_open_orders(self) -> Sequence[OrderState]:
        return ()

    def cancel_order(self, broker_order_id: str) -> None:
        raise AssertionError("no se cancela nada")


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def dependencies(
    engine: Engine,
    broker: FakeBroker,
    now: datetime | None = None,
    revision: str | None = None,
) -> live.Dependencies:
    return live.Dependencies(
        settings=ApplicationSettings.from_yaml(),
        broker_factory=lambda settings: broker,
        session_factory=sessionmaker(bind=engine),
        calendar_factory=lambda name: FakeCalendar(),
        expected_revision=lambda: revision,
        now=lambda: now or SATURDAY_NOON,
        # Nunca red en tests: la ingesta de Yahoo se sustituye por un registro.
        refresh_data=lambda symbols, end: REFRESHES.append((tuple(symbols), end)),
    )


# --- Argumentos -------------------------------------------------------------


def test_defaults() -> None:
    arguments = live.parse_args([])

    assert arguments.universe is None
    assert not arguments.check_only
    assert not arguments.once
    assert arguments.log_level == "INFO"
    assert arguments.calendar == "NYSE"


def test_dry_run_is_an_alias_of_check_only() -> None:
    assert live.parse_args(["--dry-run"]).check_only
    assert live.parse_args(["--check-only", "--universe", "initial"]).universe == (
        "initial"
    )
    assert live.parse_args(["--log-level", "debug"]).log_level == "DEBUG"


@pytest.mark.parametrize(
    "argv", [["--check-only", "--once"], ["--log-level", "TRACE"], ["--unknown"]]
)
def test_invalid_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        live.parse_args(argv)


# --- --check-only -----------------------------------------------------------


def test_check_only_reports_and_exits_without_starting_the_daemon(
    engine: Engine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    broker = FakeBroker()
    monkeypatch.setattr(
        ExecutionDaemon,
        "run",
        lambda self, max_cycles=None: pytest.fail("no debe arrancar el bucle"),
    )

    code = live.main(["--check-only"], dependencies(engine, broker))

    output = capsys.readouterr().out
    assert code == live.EXIT_OK
    assert "Cuenta Alpaca: PA… ACTIVE" in output
    assert "PA123456" not in output
    assert "Universo: diversified (15 símbolos)" in output
    assert (
        "Mercado: CERRADO; próxima apertura 2026-10-26 13:30 UTC, en 2 d 1 h 30 min"
        in output
    )
    assert "Datos diarios: AVISO" in output  # SQLite sin barras
    assert "Reconciliación: OK; posiciones broker: ninguna" in output
    assert broker.submitted == []


def test_market_open_status_is_printed(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = live.main(
        ["--check-only"], dependencies(engine, FakeBroker(), now=at(MONDAY, 15))
    )

    assert code == live.EXIT_OK
    assert (
        "Mercado: ABIERTO (sesión 2026-10-26); cierre 2026-10-26 20:00 UTC, "
        "en 5 h 0 min" in (capsys.readouterr().out)
    )


def test_inventory_drift_exits_with_mismatch_code(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker(
        positions=[
            BrokerPosition(
                symbol="MSFT", quantity=Decimal("3"), average_entry_price=Decimal("400")
            )
        ]
    )

    code = live.main(["--dry-run"], dependencies(engine, broker))

    output = capsys.readouterr().out
    assert code == live.EXIT_RECONCILIATION_MISMATCH
    assert "DESAJUSTE (envío bloqueado)" in output
    assert "MSFT: local 0, broker 3" in output


@pytest.mark.parametrize(
    ("broker", "message"),
    [
        (FakeBroker(account_number="LIVE999"), "prefijo PA"),
        (FakeBroker(blocked=True), "bloqueado"),
    ],
)
def test_account_checks_fail_before_anything_else(
    engine: Engine, capsys: pytest.CaptureFixture[str], broker: FakeBroker, message: str
) -> None:
    code = live.main(["--check-only"], dependencies(engine, broker))

    assert code == live.EXIT_PREFLIGHT_FAILED
    assert message in capsys.readouterr().out


def test_guard_failure_cancels_start(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(settings: ApplicationSettings) -> FakeBroker:
        raise LiveTradingNotAllowedError("ALPACA_PAPER debe ser exactamente 'true'")

    deps = dependencies(engine, FakeBroker()).model_copy(
        update={"broker_factory": refuse}
    )

    code = live.main(["--check-only"], deps)

    assert code == live.EXIT_PREFLIGHT_FAILED
    assert "guardia de paper trading" in capsys.readouterr().out


def test_pending_migration_cancels_start(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = live.main(
        ["--check-only"], dependencies(engine, FakeBroker(), revision="b7c8d9e0f1a2")
    )

    assert code == live.EXIT_PREFLIGHT_FAILED
    assert "alembic upgrade head" in capsys.readouterr().out


def test_unknown_universe_cancels_start(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    code = live.main(
        ["--check-only", "--universe", "nope"], dependencies(engine, FakeBroker())
    )

    assert code == live.EXIT_PREFLIGHT_FAILED
    assert "Universo desconocido" in capsys.readouterr().out


# --- --once -----------------------------------------------------------------


def test_once_runs_a_single_cycle(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    broker = FakeBroker()

    # Lunes 13:20 UTC: ventana previa a la apertura.
    code = live.main(["--once"], dependencies(engine, broker, now=at(MONDAY, 13, 20)))

    output = capsys.readouterr().out
    assert code == live.EXIT_OK
    assert "Ciclo único: post_close OK, pre_open OK" in output
    assert "Siguiente acción en" in output
    assert broker.submitted == []


def test_once_outside_any_window_does_nothing(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    deps = dependencies(engine, FakeBroker(), now=at(MONDAY, 3))

    live.main(["--once"], deps)
    assert "Ciclo único: post_close OK" in capsys.readouterr().out


# --- Daemon continuo y señales ---------------------------------------------


def test_continuous_mode_stops_gracefully_on_sigint(
    engine: Engine, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_run = ExecutionDaemon.run
    seen: list[object] = []

    def run_with_ctrl_c(self: ExecutionDaemon, max_cycles: int | None = None) -> object:
        handler = signal.getsignal(signal.SIGINT)
        seen.append(handler)
        assert callable(handler)
        handler(signal.SIGINT, None)  # Ctrl+C antes del primer ciclo
        return original_run(self, max_cycles)

    monkeypatch.setattr(ExecutionDaemon, "run", run_with_ctrl_c)

    code = live.main([], dependencies(engine, FakeBroker()))

    output = capsys.readouterr().out
    assert code == live.EXIT_OK
    assert "Daemon detenido: STOPPED (señal SIGINT)" in output
    assert seen and seen[0] is not signal.default_int_handler
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


# --- Datos diarios ----------------------------------------------------------


def test_freshness_against_the_last_closed_session(engine: Engine) -> None:
    with Session(engine) as session:
        for symbol, day in (("AAPL", date(2026, 10, 23)), ("MSFT", date(2026, 10, 22))):
            session.add(
                MarketBarRaw(
                    symbol=symbol,
                    timestamp=datetime(day.year, day.month, day.day),
                    timeframe="1d",
                    source="yfinance",
                    open=Decimal("1"),
                    high=Decimal("1"),
                    low=Decimal("1"),
                    close=Decimal("1"),
                    volume=Decimal("1"),
                )
            )
        session.commit()
        status: MarketStatus = MarketClock(FakeCalendar()).status(
            at(date(2026, 10, 24), 12)
        )

        freshness = live.check_freshness(session, ("AAPL", "MSFT", "XOM"), status)

    assert freshness.expected_session == date(2026, 10, 23)
    assert freshness.stale == {"MSFT": date(2026, 10, 22)}
    assert freshness.missing == ("XOM",)
    assert not freshness.is_current


# --- Plan diario en la CLI -------------------------------------------------


def test_check_only_previews_the_next_session_plan_without_ingesting(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    REFRESHES.clear()
    deps = dependencies(engine, FakeBroker()).model_copy(update={"refresh_data": None})

    code = live.main(["--check-only"], deps)

    output = capsys.readouterr().out
    assert code == live.EXIT_OK
    assert (
        "Plan para 2026-10-26 (señales del cierre 2026-10-23): 0 órdenes OPG" in output
    )
    assert "sin datos del cierre (no generan señal)" in output
    assert REFRESHES == []


def test_once_before_the_open_ingests_and_builds_the_plan(
    engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    REFRESHES.clear()
    broker = FakeBroker()

    code = live.main(
        ["--once", "--plan-only"], dependencies(engine, broker, now=at(MONDAY, 13, 20))
    )

    assert code == live.EXIT_OK
    assert "pre_open OK" in capsys.readouterr().out
    ((symbols, end),) = REFRESHES
    assert end == date(2026, 10, 23)
    assert len(symbols) == 15
    assert broker.submitted == []


def test_skip_ingest_flag() -> None:
    arguments = live.parse_args(["--skip-ingest", "--plan-only"])

    assert arguments.skip_ingest
    assert arguments.plan_only
