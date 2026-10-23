"""
Paper trading en vivo (Hito 4): arranca el daemon de ejecución.

Uso:

    algo-live --check-only    # verificaciones y estado del mercado, sin bucle
    algo-live --once          # verificaciones y un único ciclo del daemon
    algo-live                 # daemon continuo hasta Ctrl+C / SIGTERM
    algo-live --universe initial --log-level DEBUG

Verificaciones al arrancar (cualquier fallo termina con código 1 sin enviar
órdenes):

1. Guardia de paper trading: `ALPACA_PAPER=true`, URL de paper, clave `PK…`
   y cuenta `PA…` (`GET /v2/account`), no bloqueada.
2. MySQL accesible y con la última migración aplicada.
3. Calendario de mercado: abierto o cerrado, próxima apertura y cierre.
4. Datos diarios del universo al día (última sesión cerrada), como aviso.
5. Reconciliación inicial de inventario. Un desajuste bloquea el envío de
   órdenes y termina con código 2 (`--check-only` / `--once`); en modo
   continuo el daemon arranca bloqueado y sólo sincroniza.

Las órdenes del plan diario aún no se generan desde las señales: el daemon
sincroniza, reconcilia y publica métricas, pero `pre_open` no envía órdenes.

Ctrl+C (SIGINT), SIGTERM y SIGBREAK (Windows) piden una parada segura: la
tarea en curso termina y el daemon se detiene sin iniciar otras.
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from algo_trading.config.settings import PROJECT_ROOT, ApplicationSettings
from algo_trading.data.market_calendar import (
    DEFAULT_EXCHANGE,
    TradingCalendar,
    TradingSession,
)
from algo_trading.data.providers.dto import Timeframe
from algo_trading.data.providers.yfinance import YFINANCE_SOURCE
from algo_trading.execution.alpaca_broker import (
    LiveTradingNotAllowedError,
    PaperTradingGuard,
    build_alpaca_paper_broker,
)
from algo_trading.execution.broker import AccountSnapshot, BrokerAdapter
from algo_trading.execution.daemon import (
    DaemonState,
    ExecutionDaemon,
    ExecutionJobs,
    MarketClock,
    MarketStatus,
)
from algo_trading.execution.orders import OrderRequest
from algo_trading.execution.service import ExecutionService, ReconciliationReport
from algo_trading.persistence.repositories.market_bar_query_repository import (
    RawMarketBarQueryRepository,
)
from algo_trading.telemetry.alerts import AlertEmitter, LogAlertSink

EXIT_OK = 0
EXIT_PREFLIGHT_FAILED = 1
EXIT_RECONCILIATION_MISMATCH = 2
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class PreflightError(RuntimeError):
    """Una verificación de arranque falló: no se inicia el daemon."""


class Dependencies(BaseModel):
    """Puntos de inyección para tests; por defecto, los recursos reales."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    settings: ApplicationSettings | None = None
    broker_factory: Callable[[ApplicationSettings], Any] | None = None
    session_factory: Callable[[], Session] | None = None
    calendar_factory: Callable[[str], TradingCalendar] | None = None
    expected_revision: Callable[[], str | None] | None = None
    now: Callable[[], datetime] | None = None


class DataFreshness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_session: date | None
    stale: dict[str, date]
    missing: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return not (self.stale or self.missing)


class PreflightReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    universe: str
    symbols: tuple[str, ...]
    account: AccountSnapshot
    database_revision: str | None
    market: MarketStatus
    freshness: DataFreshness
    reconciliation: ReconciliationReport


class NoOrderPlan:
    """Plan vacío: la generación de órdenes desde señales aún no está conectada."""

    def orders_for_session(self, session: TradingSession) -> Sequence[OrderRequest]:
        logging.getLogger(__name__).info(
            "Sin plan de órdenes para %s: generación desde señales pendiente",
            session.session_date,
        )
        return ()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="algo-live",
        description="Daemon de paper trading en Alpaca Paper (nunca cuenta real).",
    )
    parser.add_argument(
        "--universe",
        default=None,
        help="Universo de la configuración (por defecto: default_universe).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-only",
        "--dry-run",
        dest="check_only",
        action="store_true",
        help="Sólo verificaciones y estado del mercado; no arranca el bucle.",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Verificaciones y un único ciclo del daemon.",
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=LOG_LEVELS,
        default="INFO",
    )
    parser.add_argument(
        "--calendar",
        default=DEFAULT_EXCHANGE,
        help="Calendario de pandas-market-calendars (por defecto NYSE).",
    )
    return parser.parse_args(argv)


def run_preflight(
    arguments: argparse.Namespace,
    settings: ApplicationSettings,
    broker: BrokerAdapter,
    service: ExecutionService,
    session: Session,
    market: MarketClock,
    alerts: AlertEmitter,
    expected_revision: str | None,
    now: datetime,
) -> PreflightReport:
    universe = (arguments.universe or settings.default_universe).lower()
    symbols = settings.universe(universe)

    account = broker.get_account()
    PaperTradingGuard.check_account(account)

    if account.trading_blocked:
        raise PreflightError("La cuenta paper tiene el trading bloqueado")

    try:
        session.execute(text("SELECT 1"))
        revision = MigrationContext.configure(
            session.connection()
        ).get_current_revision()
    except Exception as error:
        raise PreflightError(f"MySQL no disponible: {error}") from error

    if expected_revision is not None and revision != expected_revision:
        raise PreflightError(
            f"Migración de MySQL {revision} distinta de la última "
            f"({expected_revision}): ejecute alembic upgrade head"
        )

    status = market.status(now)
    freshness = check_freshness(session, symbols, status)
    reconciliation = service.reconcile_positions(alerts)

    return PreflightReport(
        universe=universe,
        symbols=symbols,
        account=account,
        database_revision=revision,
        market=status,
        freshness=freshness,
        reconciliation=reconciliation,
    )


def check_freshness(
    session: Session, symbols: Sequence[str], status: MarketStatus
) -> DataFreshness:
    """Última barra diaria raw de cada símbolo frente a la última sesión cerrada."""
    expected = status.previous_session.session_date if status.previous_session else None
    repository = RawMarketBarQueryRepository(session)
    stale: dict[str, date] = {}
    missing: list[str] = []

    for symbol in symbols:
        latest = repository.get_latest_timestamp(
            symbol, Timeframe.DAILY.value, YFINANCE_SOURCE
        )

        if latest is None:
            missing.append(symbol)
        elif expected is not None and latest.date() < expected:
            stale[symbol] = latest.date()

    return DataFreshness(expected_session=expected, stale=stale, missing=tuple(missing))


def format_preflight(report: PreflightReport) -> str:
    account = report.account
    market = report.market
    reconciliation = report.reconciliation
    freshness = report.freshness
    lines = [
        "Verificaciones de arranque (paper trading)",
        f"  Cuenta Alpaca: {account.account_number[:2]}… {account.status}  "
        f"equity {account.equity:,.2f} {account.currency}  "
        f"poder de compra {account.buying_power:,.2f}",
        f"  MySQL: migración {report.database_revision}",
        f"  Universo: {report.universe} ({len(report.symbols)} símbolos)",
    ]

    if market.is_open and market.current_session is not None:
        lines.append(
            f"  Mercado: ABIERTO (sesión {market.current_session.session_date}); "
            f"cierre {market.next_close:%Y-%m-%d %H:%M} UTC, en "
            f"{_duration(market.time_to_next_close)}"
        )
    else:
        early = " (cierre temprano)" if market.next_session.is_early_close else ""
        lines.append(
            f"  Mercado: CERRADO; próxima apertura {market.next_open:%Y-%m-%d %H:%M} "
            f"UTC, en {_duration(market.time_to_next_open)}{early}"
        )

    if freshness.is_current:
        lines.append(f"  Datos diarios: al día ({freshness.expected_session})")
    else:
        details = [f"{s} {d}" for s, d in sorted(freshness.stale.items())]
        details += [f"{s} sin datos" for s in freshness.missing]
        lines.append(
            f"  Datos diarios: AVISO, no llegan a {freshness.expected_session}: "
            + ", ".join(details)
        )

    positions = ", ".join(
        f"{symbol} {quantity}"
        for symbol, quantity in reconciliation.broker_positions.items()
    )
    lines.append(
        "  Reconciliación: "
        + (
            "OK"
            if reconciliation.is_consistent
            else "DESAJUSTE (envío bloqueado)"
            if reconciliation.requires_halt
            else "avisos"
        )
        + f"; posiciones broker: {positions or 'ninguna'}"
    )

    for drift in reconciliation.drifts:
        lines.append(
            f"    {drift.symbol}: local {drift.local_quantity}, broker "
            f"{drift.broker_quantity}"
        )

    for label, items in (
        ("fills sin registrar", reconciliation.unrecorded_fills),
        ("órdenes del broker desconocidas", reconciliation.unknown_broker_orders),
        ("órdenes locales sin abrir en el broker", reconciliation.stale_local_orders),
    ):
        if items:
            lines.append(f"    {label}: {', '.join(items)}")

    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None, dependencies: Dependencies | None = None
) -> int:
    arguments = parse_args(argv)
    deps = dependencies or Dependencies()
    _configure_output(arguments.log_level)
    log = logging.getLogger("algo_trading.live")

    settings = deps.settings or ApplicationSettings.from_yaml()
    now = deps.now or (lambda: datetime.now(UTC))
    alerts = AlertEmitter(LogAlertSink(), clock=now)

    try:
        broker = (deps.broker_factory or build_alpaca_paper_broker)(settings)
    except LiveTradingNotAllowedError as error:
        print(f"Arranque cancelado (guardia de paper trading): {error}")
        return EXIT_PREFLIGHT_FAILED

    session = (deps.session_factory or _default_session_factory())()

    try:
        calendar = (deps.calendar_factory or _exchange_calendar)(arguments.calendar)
        market = MarketClock(calendar)
        service = ExecutionService(broker, session, clock=now)

        try:
            report = run_preflight(
                arguments,
                settings,
                broker,
                service,
                session,
                market,
                alerts,
                (deps.expected_revision or _alembic_head)(),
                now(),
            )
        except (PreflightError, LiveTradingNotAllowedError, ValueError) as error:
            print(f"Arranque cancelado: {error}")
            return EXIT_PREFLIGHT_FAILED

        print(format_preflight(report))
        mismatch = report.reconciliation.requires_halt

        if arguments.check_only:
            return EXIT_RECONCILIATION_MISMATCH if mismatch else EXIT_OK

        jobs = ExecutionJobs(
            service,
            NoOrderPlan(),
            alerts,
            request_log=(
                (lambda: broker.request_log) if hasattr(broker, "request_log") else None
            ),
        )
        daemon = ExecutionDaemon(market, jobs, alerts=alerts, now=now)

        if arguments.once:
            runs = daemon.run_pending()
            print(
                "Ciclo único: "
                + (
                    ", ".join(f"{run.job.value} {run.outcome.value}" for run in runs)
                    or "ninguna tarea vencida"
                )
            )
            print(
                f"Siguiente acción en {daemon.seconds_until_next_action():.0f} s "
                "(acotado al intervalo máximo de espera)"
            )
            return EXIT_RECONCILIATION_MISMATCH if mismatch else EXIT_OK

        if mismatch:
            log.warning("Daemon arrancado con el envío de órdenes bloqueado")

        restore = daemon.install_signal_handlers()
        print("Daemon en marcha; Ctrl+C para una parada segura.")

        try:
            state = daemon.run()
        finally:
            restore()

        print(f"Daemon detenido: {state.value} ({daemon.stop_reason or 'sin motivo'})")
        return EXIT_PREFLIGHT_FAILED if state is DaemonState.HALTED else EXIT_OK
    finally:
        session.close()


def _configure_output(level: str) -> None:
    # La consola de Windows usa cp1252 al redirigir la salida: se fuerza UTF-8
    # para no fallar con los caracteres del informe (→, …).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)

        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    # Marcas del log en UTC, como las alertas y MySQL.
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S"
    )
    formatter.converter = time.gmtime
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logging.basicConfig(level=getattr(logging, level), handlers=[handler], force=True)
    logging.getLogger("alembic").setLevel(logging.WARNING)


def _default_session_factory() -> Callable[[], Session]:
    from algo_trading.persistence.database import SessionFactory

    return SessionFactory


def _exchange_calendar(name: str) -> TradingCalendar:
    from algo_trading.data.market_calendar import ExchangeCalendar

    return ExchangeCalendar(name.upper())


def _alembic_head() -> str | None:
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI))).get_current_head()


def _duration(value: Any) -> str:
    seconds = int(value.total_seconds())
    days, remainder = divmod(max(seconds, 0), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    return f"{days} d {hours} h {minutes} min" if days else f"{hours} h {minutes} min"


if __name__ == "__main__":
    raise SystemExit(main())
