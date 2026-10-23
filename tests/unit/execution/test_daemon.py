import signal
import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from algo_trading.data.market_calendar import TradingSession
from algo_trading.execution.alpaca_broker import LiveTradingNotAllowedError
from algo_trading.execution.daemon import (
    DaemonSchedule,
    DaemonState,
    ExecutionDaemon,
    JobName,
    JobOutcome,
    MarketClock,
)
from algo_trading.telemetry import AlertEmitter, MemoryAlertSink

FRIDAY = date(2026, 10, 23)
MONDAY = date(2026, 10, 26)
TUESDAY = date(2026, 10, 27)


def session(day: date, close_hour: int = 20) -> TradingSession:
    return TradingSession(
        session_date=day,
        market_open=datetime(day.year, day.month, day.day, 13, 30, tzinfo=UTC),
        market_close=datetime(day.year, day.month, day.day, close_hour, 0, tzinfo=UTC),
        is_early_close=close_hour != 20,
    )


class FakeCalendar:
    """Lunes a viernes; el miércoles 28 es festivo y el jueves cierra antes."""

    name = "TEST"

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        days = []
        day = start

        while day <= end:
            if day.weekday() < 5 and day != date(2026, 10, 28):
                days.append(session(day, 17 if day == date(2026, 10, 29) else 20))
            day += timedelta(days=1)

        return tuple(days)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingJobs:
    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, date]] = []
        self.fail = fail

    def pre_open(self, session: TradingSession) -> None:
        self._record("pre_open", session)

    def intraday(self, session: TradingSession) -> None:
        self._record("intraday", session)

    def post_close(self, session: TradingSession) -> None:
        self._record("post_close", session)

    def _record(self, job: str, session: TradingSession) -> None:
        self.calls.append((job, session.session_date))

        if self.fail is not None:
            raise self.fail


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def daemon(
    clock: Clock, jobs: RecordingJobs, sink: MemoryAlertSink | None = None
) -> ExecutionDaemon:
    return ExecutionDaemon(
        MarketClock(FakeCalendar()),
        jobs,
        schedule=DaemonSchedule(),
        alerts=AlertEmitter(sink, clock=clock) if sink is not None else None,
        now=clock,
    )


# --- Reloj de mercado -------------------------------------------------------


def test_market_status_while_open_and_closed() -> None:
    market = MarketClock(FakeCalendar())

    open_status = market.status(at(MONDAY, 15))
    closed = market.status(at(date(2026, 10, 24), 12))  # sábado

    assert open_status.is_open
    assert open_status.current_session == session(MONDAY)
    assert open_status.next_close == at(MONDAY, 20)
    assert open_status.time_to_next_close == timedelta(hours=5)
    assert open_status.next_open == at(TUESDAY, 13, 30)
    assert not closed.is_open
    assert closed.current_session is None
    assert closed.previous_session is not None
    assert closed.previous_session.session_date == date(2026, 10, 23)
    assert closed.next_open == at(MONDAY, 13, 30)
    assert closed.time_to_next_open == timedelta(days=2, hours=1, minutes=30)


def test_holidays_and_early_closes() -> None:
    market = MarketClock(FakeCalendar())

    holiday = market.status(at(date(2026, 10, 28), 15))
    early = market.status(at(date(2026, 10, 29), 16, 59))
    after_early = market.status(at(date(2026, 10, 29), 17))

    assert not holiday.is_open
    assert holiday.next_open == at(date(2026, 10, 29), 13, 30)
    assert early.is_open
    assert early.next_close == at(date(2026, 10, 29), 17)
    assert not after_early.is_open


def test_naive_now_is_treated_as_utc() -> None:
    status = MarketClock(FakeCalendar()).status(datetime(2026, 10, 26, 15))

    assert status.is_open


# --- Programación de tareas -------------------------------------------------


def test_full_session_cycle() -> None:
    clock = Clock(at(MONDAY, 13, 0))
    jobs = RecordingJobs()
    execution = daemon(clock, jobs)

    timeline = [
        # Al arrancar reconcilia la última sesión cerrada (viernes).
        (at(MONDAY, 13, 0), [("post_close", FRIDAY)]),
        (at(MONDAY, 13, 16), [("pre_open", MONDAY)]),
        (at(MONDAY, 13, 20), []),  # ya enviada para esta sesión
        (at(MONDAY, 13, 31), [("intraday", MONDAY)]),
        (
            at(
                MONDAY,
                13,
                31,
            ),
            [],
        ),  # intervalo de 60 s no cumplido
        (at(MONDAY, 13, 32), [("intraday", MONDAY)]),
        (at(MONDAY, 20, 5), []),  # cerrado, antes de cierre + 15 min
        (at(MONDAY, 20, 15), [("post_close", MONDAY)]),
        (at(MONDAY, 23, 0), []),
    ]

    for moment, expected in timeline:
        clock.now = moment
        before = len(jobs.calls)
        execution.run_pending()
        assert jobs.calls[before:] == expected, moment


def test_pre_open_is_missed_if_the_daemon_starts_after_the_open() -> None:
    clock = Clock(at(MONDAY, 15))
    jobs = RecordingJobs()
    sink = MemoryAlertSink()
    execution = daemon(clock, jobs, sink)

    runs = execution.run_pending()

    assert [(r.job, r.outcome) for r in runs] == [
        (JobName.POST_CLOSE, JobOutcome.OK),
        (JobName.PRE_OPEN, JobOutcome.MISSED),
        (JobName.INTRADAY, JobOutcome.OK),
    ]
    assert ("pre_open", MONDAY) not in jobs.calls
    assert "JOB_MISSED" in sink.codes()


def test_startup_after_close_reconciles_the_last_session_once() -> None:
    clock = Clock(at(date(2026, 10, 24), 12))  # sábado
    jobs = RecordingJobs()
    execution = daemon(clock, jobs)

    execution.run_pending()
    execution.run_pending()

    assert jobs.calls == [("post_close", FRIDAY)]


def test_job_failures_are_recorded_and_the_daemon_continues() -> None:
    clock = Clock(at(MONDAY, 13, 16))
    jobs = RecordingJobs(fail=RuntimeError("broker caído"))
    sink = MemoryAlertSink()
    execution = daemon(clock, jobs, sink)

    runs = execution.run_pending()

    assert [r.job for r in runs] == [JobName.POST_CLOSE, JobName.PRE_OPEN]
    run = runs[-1]
    assert run.outcome is JobOutcome.FAILED
    assert run.error == "RuntimeError: broker caído"
    assert execution.state is DaemonState.IDLE
    assert "JOB_FAILED" in sink.codes()


def test_fatal_errors_halt_the_daemon() -> None:
    clock = Clock(at(MONDAY, 13, 16))
    jobs = RecordingJobs(fail=LiveTradingNotAllowedError("ALPACA_PAPER no es true"))
    execution = daemon(clock, jobs)

    state = execution.run()

    assert state is DaemonState.HALTED
    assert execution.stop_reason is not None
    assert "LiveTradingNotAllowedError" in execution.stop_reason


def test_wait_until_next_action() -> None:
    clock = Clock(at(MONDAY, 12, 0))
    execution = daemon(clock, RecordingJobs())

    # Siguiente acción 13:15 (pre_open), acotada por max_wait = 5 min.
    assert execution.seconds_until_next_action() == 300

    clock.now = at(MONDAY, 13, 12)
    assert execution.seconds_until_next_action() == 180

    clock.now = at(MONDAY, 13, 31)
    execution.run_pending()
    assert execution.seconds_until_next_action() == 60


# --- Parada segura ----------------------------------------------------------


def test_request_stop_ends_the_loop_after_the_current_cycle() -> None:
    clock = Clock(at(MONDAY, 15))
    jobs = RecordingJobs()
    sink = MemoryAlertSink()
    execution = daemon(clock, jobs, sink)

    class StopAfterIntraday(RecordingJobs):
        def intraday(self, session: TradingSession) -> None:
            super().intraday(session)
            execution.request_stop("prueba")

    execution._jobs = StopAfterIntraday()  # noqa: SLF001
    state = execution.run()

    assert state is DaemonState.STOPPED
    assert execution.stop_reason == "prueba"
    assert sink.codes()[0] == "DAEMON_STARTED"
    assert sink.codes()[-1] == "DAEMON_STOPPED"


def test_stop_interrupts_a_long_wait() -> None:
    clock = Clock(at(date(2026, 10, 24), 12))
    stop = threading.Event()
    execution = ExecutionDaemon(
        MarketClock(FakeCalendar()),
        RecordingJobs(),
        schedule=DaemonSchedule(max_wait=timedelta(hours=1)),
        now=clock,
        stop_event=stop,
    )
    timer = threading.Timer(0.05, execution.request_stop, args=("SIGTERM simulada",))
    timer.start()

    started = datetime.now(UTC)
    state = execution.run()

    assert state is DaemonState.STOPPED
    assert datetime.now(UTC) - started < timedelta(seconds=5)


def test_signal_handlers_request_a_graceful_stop() -> None:
    execution = daemon(Clock(at(MONDAY, 12)), RecordingJobs())
    restore = execution.install_signal_handlers()

    try:
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        handler(signal.SIGINT, None)
    finally:
        restore()

    assert execution.stop_reason == "señal SIGINT"
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_stopped_daemon_starts_no_new_jobs() -> None:
    clock = Clock(at(MONDAY, 13, 16))
    jobs = RecordingJobs()
    execution = daemon(clock, jobs)
    execution.request_stop()

    execution.run_pending()

    assert jobs.calls == []


@pytest.mark.parametrize("days", [0, 6])
def test_market_clock_needs_a_week_of_lookahead(days: int) -> None:
    with pytest.raises(ValueError, match="semana"):
        MarketClock(FakeCalendar(), lookahead_days=days)
