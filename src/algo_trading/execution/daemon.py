"""
Daemon de ejecución (Hito 4): ciclo diario del paper trading según el
calendario de mercado.

Para cada sesión del calendario (NYSE por defecto, horarios en UTC con
festivos y cierres tempranos):

1. `pre_open`: una vez, en `[apertura - pre_open_lead, apertura)`. Envía las
   órdenes `OPG` de la sesión (Alpaca las acepta hasta poco antes de la
   subasta). Si el daemon arranca con la sesión ya abierta, la tarea se da por
   perdida (`MISSED`): las órdenes de apertura ya no son válidas.
2. `intraday`: cada `intraday_interval` mientras el mercado está abierto,
   desde `apertura + intraday_start_delay`. Sincroniza órdenes y fills.
3. `post_close`: una vez, desde `cierre + post_close_delay`, para la última
   sesión cerrada. Reconcilia inventario, calcula métricas y prepara los
   cierres o reajustes del día siguiente.

Cada tarea se ejecuta como mucho una vez por sesión (salvo `intraday`). Un
error en una tarea se registra y alerta, y el daemon sigue; los errores
fatales (p. ej. `LiveTradingNotAllowedError`) lo detienen en estado `HALTED`.

Parada segura: SIGINT / SIGTERM (y SIGBREAK en Windows) sólo activan un
evento de parada; la tarea en curso termina, no empieza ninguna nueva y el
daemon acaba en `STOPPED`. La espera entre ciclos usa ese evento, así que la
parada es inmediata aunque falten horas para la siguiente tarea.
"""

import signal
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from types import FrameType
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.data.market_calendar import TradingCalendar, TradingSession
from algo_trading.execution.alpaca_broker import LiveTradingNotAllowedError
from algo_trading.execution.orders import OrderRequest
from algo_trading.execution.service import (
    ExecutionService,
    ReconciliationReport,
    TradingHaltedError,
)
from algo_trading.telemetry.alerts import AlertEmitter, AlertLevel
from algo_trading.telemetry.metrics import (
    LatencyTracker,
    OperationalMetrics,
    operational_metrics,
)


class MarketStatus(BaseModel):
    """Situación del mercado en `now` (todos los instantes en UTC)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: datetime
    is_open: bool
    current_session: TradingSession | None
    previous_session: TradingSession | None
    next_session: TradingSession
    next_open: datetime
    next_close: datetime

    @property
    def time_to_next_open(self) -> timedelta:
        return self.next_open - self.now

    @property
    def time_to_next_close(self) -> timedelta:
        return self.next_close - self.now


class MarketClock:
    """Apertura, cierre y sesión en curso a partir del calendario de mercado."""

    def __init__(self, calendar: TradingCalendar, lookahead_days: int = 14) -> None:
        if lookahead_days < 7:
            raise ValueError("lookahead_days debe cubrir al menos una semana")

        self._calendar = calendar
        self._lookahead = timedelta(days=lookahead_days)
        self._cache: dict[tuple[date, date], tuple[TradingSession, ...]] = {}

    def status(self, now: datetime) -> MarketStatus:
        now = _utc(now)
        sessions = self._sessions(now)
        current = next(
            (s for s in sessions if s.market_open <= now < s.market_close), None
        )
        previous = next((s for s in reversed(sessions) if s.market_close <= now), None)
        upcoming = [s for s in sessions if s.market_open > now]

        if not upcoming:
            raise ValueError("El calendario no tiene sesiones futuras en el horizonte")

        return MarketStatus(
            now=now,
            is_open=current is not None,
            current_session=current,
            previous_session=previous,
            next_session=upcoming[0],
            next_open=upcoming[0].market_open,
            next_close=(current or upcoming[0]).market_close,
        )

    def _sessions(self, now: datetime) -> tuple[TradingSession, ...]:
        # Margen de días a ambos lados: la fecha de sesión es la de la bolsa,
        # no la UTC.
        key = ((now - timedelta(days=7)).date(), (now + self._lookahead).date())

        if key not in self._cache:
            self._cache = {key: self._calendar.sessions(*key)}

        return self._cache[key]


class DaemonJobs(Protocol):
    def pre_open(self, session: TradingSession) -> None: ...

    def intraday(self, session: TradingSession) -> None: ...

    def post_close(self, session: TradingSession) -> None: ...


class DaemonSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pre_open_lead: timedelta = Field(default=timedelta(minutes=15))
    intraday_start_delay: timedelta = Field(default=timedelta(minutes=1))
    intraday_interval: timedelta = Field(default=timedelta(seconds=60))
    post_close_delay: timedelta = Field(default=timedelta(minutes=15))
    max_wait: timedelta = Field(default=timedelta(minutes=5))


class DaemonState(StrEnum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    HALTED = "HALTED"


class JobName(StrEnum):
    PRE_OPEN = "pre_open"
    INTRADAY = "intraday"
    POST_CLOSE = "post_close"


class JobOutcome(StrEnum):
    OK = "OK"
    FAILED = "FAILED"
    MISSED = "MISSED"


class JobRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    job: JobName
    session_date: str
    started_at: datetime
    finished_at: datetime
    outcome: JobOutcome
    error: str | None = None


class ExecutionDaemon:
    def __init__(
        self,
        market: MarketClock,
        jobs: DaemonJobs,
        schedule: DaemonSchedule | None = None,
        alerts: AlertEmitter | None = None,
        now: Callable[[], datetime] | None = None,
        stop_event: threading.Event | None = None,
        fatal_errors: tuple[type[Exception], ...] = (LiveTradingNotAllowedError,),
    ) -> None:
        self._market = market
        self._jobs = jobs
        self._schedule = schedule or DaemonSchedule()
        self._alerts = alerts
        self._now = now or (lambda: datetime.now(UTC))
        self._stop = stop_event or threading.Event()
        self._fatal_errors = fatal_errors
        self._state = DaemonState.IDLE
        self._stop_reason: str | None = None
        self._done: set[tuple[JobName, str]] = set()
        self._last_intraday: datetime | None = None
        self._history: list[JobRun] = []

    @property
    def state(self) -> DaemonState:
        return self._state

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    @property
    def history(self) -> tuple[JobRun, ...]:
        return tuple(self._history)

    def request_stop(self, reason: str = "parada solicitada") -> None:
        """Parada segura: termina la tarea en curso y no inicia otras."""
        if self._stop_reason is None:
            self._stop_reason = reason

        if self._state is DaemonState.RUNNING:
            self._state = DaemonState.STOPPING

        self._stop.set()

    def graceful_shutdown(self, reason: str = "parada segura solicitada") -> None:
        """Alias de `request_stop` para quien controla el daemon (CLI, señales)."""
        self.request_stop(reason)

    def install_signal_handlers(self) -> Callable[[], None]:
        """
        Instala SIGINT, SIGTERM y SIGBREAK (Windows). Devuelve una función que
        restaura los manejadores anteriores. Sólo desde el hilo principal.
        """
        previous: dict[int, Any] = {}

        def handler(signum: int, frame: FrameType | None) -> None:
            self.graceful_shutdown(f"señal {signal.Signals(signum).name}")

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            number = getattr(signal, name, None)

            if number is not None:
                previous[number] = signal.signal(number, handler)

        def restore() -> None:
            for number, old in previous.items():
                signal.signal(number, old)

        return restore

    def run(self, max_cycles: int | None = None) -> DaemonState:
        """Bucle principal hasta la parada (o `max_cycles` ciclos)."""
        self._state = DaemonState.RUNNING
        self._alert(AlertLevel.INFO, "DAEMON_STARTED", "Daemon de ejecución iniciado")
        cycles = 0

        while not self._stop.is_set():
            self.run_pending()

            if self._state is DaemonState.HALTED:
                break

            cycles += 1

            if max_cycles is not None and cycles >= max_cycles:
                break

            self._stop.wait(self.seconds_until_next_action())

        if self._state is not DaemonState.HALTED:
            self._state = DaemonState.STOPPED

        self._alert(
            AlertLevel.WARNING
            if self._state is DaemonState.HALTED
            else AlertLevel.INFO,
            f"DAEMON_{self._state.value}",
            f"Daemon detenido: {self._stop_reason or 'fin del bucle'}",
        )
        return self._state

    def run_pending(self) -> tuple[JobRun, ...]:
        """Ejecuta las tareas vencidas en `now` (orden: cierre, apertura, sesión)."""
        runs: list[JobRun] = []
        status = self._market.status(self._now())
        schedule = self._schedule

        previous = status.previous_session
        if previous is not None and not self._is_done(JobName.POST_CLOSE, previous):
            if (
                status.now >= previous.market_close + schedule.post_close_delay
                and not self._stop.is_set()
            ):
                runs.append(self._execute(JobName.POST_CLOSE, previous))

        upcoming = status.current_session or status.next_session
        if not self._is_done(JobName.PRE_OPEN, upcoming) and not self._stop.is_set():
            if status.now >= upcoming.market_open:
                runs.append(self._missed(JobName.PRE_OPEN, upcoming))
            elif status.now >= upcoming.market_open - schedule.pre_open_lead:
                runs.append(self._execute(JobName.PRE_OPEN, upcoming))

        current = status.current_session
        if current is not None and self._intraday_due(current, status.now):
            if not self._stop.is_set():
                self._last_intraday = status.now
                runs.append(self._execute(JobName.INTRADAY, current, once=False))

        return tuple(runs)

    def seconds_until_next_action(self) -> float:
        status = self._market.status(self._now())
        schedule = self._schedule
        candidates: list[datetime] = []
        upcoming = status.current_session or status.next_session

        if not self._is_done(JobName.PRE_OPEN, upcoming):
            candidates.append(upcoming.market_open - schedule.pre_open_lead)

        if status.current_session is not None:
            current = status.current_session
            first = current.market_open + schedule.intraday_start_delay
            candidates.append(
                first
                if self._last_intraday is None or self._last_intraday < first
                else self._last_intraday + schedule.intraday_interval
            )
            candidates.append(current.market_close + schedule.post_close_delay)
        elif status.previous_session is not None and not self._is_done(
            JobName.POST_CLOSE, status.previous_session
        ):
            candidates.append(
                status.previous_session.market_close + schedule.post_close_delay
            )

        candidates.append(status.next_open - schedule.pre_open_lead)
        future = [moment for moment in candidates if moment > status.now]
        wait = min(future) - status.now if future else timedelta(0)
        return max(0.0, min(wait, schedule.max_wait).total_seconds())

    def _intraday_due(self, session: TradingSession, now: datetime) -> bool:
        schedule = self._schedule

        if now < session.market_open + schedule.intraday_start_delay:
            return False

        return (
            self._last_intraday is None
            or self._last_intraday < session.market_open
            or now - self._last_intraday >= schedule.intraday_interval
        )

    def _is_done(self, job: JobName, session: TradingSession) -> bool:
        return (job, session.session_date.isoformat()) in self._done

    def _execute(
        self, job: JobName, session: TradingSession, once: bool = True
    ) -> JobRun:
        started = self._now()
        error: str | None = None
        outcome = JobOutcome.OK

        try:
            getattr(self._jobs, job.value)(session)
        except Exception as exc:  # se registra y alerta; el daemon sigue
            outcome = JobOutcome.FAILED
            error = f"{type(exc).__name__}: {exc}"
            fatal = isinstance(exc, self._fatal_errors)
            self._alert(
                AlertLevel.CRITICAL,
                "JOB_FAILED",
                f"{job.value} {session.session_date}: {error}",
                {"job": job.value, "fatal": fatal},
            )

            if fatal:
                self._state = DaemonState.HALTED
                self._stop_reason = f"error fatal en {job.value}: {error}"
                self._stop.set()

        if once:
            self._done.add((job, session.session_date.isoformat()))

        run = JobRun(
            job=job,
            session_date=session.session_date.isoformat(),
            started_at=started,
            finished_at=self._now(),
            outcome=outcome,
            error=error,
        )
        self._history.append(run)
        return run

    def _missed(self, job: JobName, session: TradingSession) -> JobRun:
        self._done.add((job, session.session_date.isoformat()))
        now = self._now()
        self._alert(
            AlertLevel.WARNING,
            "JOB_MISSED",
            f"{job.value} {session.session_date}: la sesión ya había abierto",
        )
        run = JobRun(
            job=job,
            session_date=session.session_date.isoformat(),
            started_at=now,
            finished_at=now,
            outcome=JobOutcome.MISSED,
        )
        self._history.append(run)
        return run

    def _alert(
        self,
        level: AlertLevel,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        if self._alerts is not None:
            self._alerts.emit(level, code, message, details)


class OrderPlanSource(Protocol):
    """Órdenes `OPG` a enviar antes de la apertura de una sesión."""

    def orders_for_session(self, session: TradingSession) -> Sequence[OrderRequest]: ...


class ExecutionJobs:
    """
    Tareas del daemon sobre `ExecutionService`:

    - `pre_open`: envía las órdenes del plan de la sesión (no envía nada si la
      reconciliación bloqueó el envío; lo alerta).
    - `intraday`: sincroniza órdenes y fills abiertos.
    - `post_close`: reconcilia y publica las métricas operativas del día
      (slippage, latencia acumulada de la API y tasa de ejecución).
    """

    def __init__(
        self,
        service: ExecutionService,
        plans: OrderPlanSource,
        alerts: AlertEmitter,
        latency: LatencyTracker | None = None,
        request_log: Callable[[], Sequence[Any]] | None = None,
    ) -> None:
        self._service = service
        self._plans = plans
        self._alerts = alerts
        self._latency = latency or LatencyTracker()
        self._request_log = request_log
        self.last_reconciliation: ReconciliationReport | None = None
        self.last_metrics: OperationalMetrics | None = None

    def pre_open(self, session: TradingSession) -> None:
        orders = self._plans.orders_for_session(session)

        for request in orders:
            try:
                self._service.submit(request)
            except TradingHaltedError as error:
                self._alerts.emit(
                    AlertLevel.CRITICAL,
                    "ORDERS_BLOCKED",
                    str(error),
                    {"pending_orders": len(orders)},
                )
                return

        self._refresh_latency()
        self._alerts.emit(
            AlertLevel.INFO,
            "PRE_OPEN_SUBMITTED",
            f"{len(orders)} órdenes enviadas para {session.session_date}",
        )

    def intraday(self, session: TradingSession) -> None:
        self._service.sync_open_orders()
        self._refresh_latency()

    def post_close(self, session: TradingSession) -> None:
        self.last_reconciliation = self._service.reconcile_positions(self._alerts)
        self._refresh_latency()
        self.last_metrics = operational_metrics(
            self._service.order_records(), self._latency.timings
        )
        metrics = self.last_metrics
        self._alerts.emit(
            AlertLevel.INFO,
            "DAILY_SUMMARY",
            f"Resumen operativo {session.session_date}",
            {
                "slippage_mean_bps": _text(metrics.slippage.mean_bps),
                "slippage_weighted_bps": _text(metrics.slippage.weighted_bps),
                "latency_p50_ms": metrics.latency.p50_ms,
                "latency_p95_ms": metrics.latency.p95_ms,
                "api_requests": metrics.latency.requests,
                "api_errors": metrics.latency.errors,
                "fill_rate": metrics.orders.fill_rate,
                "open_orders": metrics.orders.open,
            },
        )

    def _refresh_latency(self) -> None:
        if self._request_log is not None:
            self._latency.update(self._request_log())


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _text(value: object) -> str | None:
    return None if value is None else str(value)
