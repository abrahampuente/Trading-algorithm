"""
Persistencia del estado operativo del daemon (Hito 4).

- `SqlProtectionStore`: entradas a la espera de su OCO de protección, para
  retomarlas tras un reinicio del daemon.
- `DatabaseAlertSink`: destino de alertas que las guarda en
  `operational_alerts`; un fallo de base de datos nunca interrumpe el trading.
- `TelemetryStore`: métricas diarias (`daily_operational_metrics`, una fila
  por sesión, reemplazada si se recalcula) y lectura de alertas.

Cada operación usa su propia sesión y transacción corta (`session_factory`),
independiente de la del `ExecutionService`. Timestamps en UTC sin zona.
"""

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from algo_trading.persistence.models.live_state import (
    DailyOperationalMetricsModel,
    OperationalAlertModel,
    PendingProtectionModel,
)
from algo_trading.telemetry.alerts import Alert, AlertLevel
from algo_trading.telemetry.metrics import OperationalMetrics

_log = logging.getLogger(__name__)


class ProtectionResolution(StrEnum):
    PROTECTED = "PROTECTED"  # entrada ejecutada y OCO enviada
    NOT_FILLED = "NOT_FILLED"  # entrada cerrada sin ejecución (gap)
    NOT_SUBMITTED = "NOT_SUBMITTED"  # la entrada nunca llegó al broker


class ProtectionRecord(BaseModel):
    """Datos para recalcular la OCO sobre el precio ejecutado de la entrada."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entry_client_order_id: str
    symbol: str
    session_date: date
    entry_atr: Decimal
    stop_loss_atr_multiple: Decimal
    take_profit_atr_multiple: Decimal
    signal_timestamp: datetime | None = None
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: ProtectionResolution | None = None
    exit_client_order_id: str | None = None


class SqlProtectionStore:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def add(self, record: ProtectionRecord) -> None:
        """Registra la entrada; si ya estaba (mismo `client_order_id`), no hace nada."""
        with self._session_factory() as session:
            exists = session.scalar(
                select(PendingProtectionModel.id).where(
                    PendingProtectionModel.entry_client_order_id
                    == record.entry_client_order_id
                )
            )

            if exists is not None:
                return

            session.add(
                PendingProtectionModel(
                    entry_client_order_id=record.entry_client_order_id,
                    symbol=record.symbol,
                    session_date=record.session_date,
                    entry_atr=record.entry_atr,
                    stop_loss_atr_multiple=record.stop_loss_atr_multiple,
                    take_profit_atr_multiple=record.take_profit_atr_multiple,
                    signal_timestamp=_to_db(record.signal_timestamp),
                    created_at=_to_db(record.created_at or self._clock()),
                )
            )
            session.commit()

    def pending(self) -> tuple[ProtectionRecord, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(PendingProtectionModel)
                .where(PendingProtectionModel.resolution.is_(None))
                .order_by(PendingProtectionModel.id)
            ).all()
            return tuple(_to_record(row) for row in rows)

    def resolve(
        self,
        entry_client_order_id: str,
        resolution: ProtectionResolution,
        exit_client_order_id: str | None = None,
    ) -> None:
        with self._session_factory() as session:
            row = session.scalar(
                select(PendingProtectionModel).where(
                    PendingProtectionModel.entry_client_order_id
                    == entry_client_order_id
                )
            )

            if row is None:
                raise LookupError(entry_client_order_id)

            row.resolution = resolution.value
            row.resolved_at = _to_db(self._clock())
            row.exit_client_order_id = exit_client_order_id
            session.commit()

    def get(self, entry_client_order_id: str) -> ProtectionRecord | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(PendingProtectionModel).where(
                    PendingProtectionModel.entry_client_order_id
                    == entry_client_order_id
                )
            )
            return _to_record(row) if row is not None else None


class DatabaseAlertSink:
    """Guarda cada alerta en `operational_alerts`; los fallos sólo se registran."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory
        self.failed = 0

    def emit(self, alert: Alert) -> None:
        try:
            with self._session_factory() as session:
                session.add(
                    OperationalAlertModel(
                        created_at=_to_db(alert.timestamp),
                        level=alert.level.value,
                        code=alert.code,
                        message=alert.message,
                        details=alert.model_dump(mode="json")["details"],
                    )
                )
                session.commit()
        except Exception as error:  # el trading no depende de guardar la alerta
            self.failed += 1
            _log.warning(
                "Alerta %s no guardada en MySQL: %s", alert.code, type(error).__name__
            )


class StoredDailyMetrics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: date
    computed_at: datetime
    slippage_orders: int
    slippage_mean_bps: Decimal | None
    api_requests: int
    api_errors: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    orders_total: int
    orders_open: int
    fill_rate: float | None
    reconciliation_consistent: bool | None
    payload: dict[str, Any]


class TelemetryStore:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def record_daily_metrics(
        self,
        session_date: date,
        metrics: OperationalMetrics,
        reconciliation_consistent: bool | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Guarda (o reemplaza) las métricas de la sesión."""
        payload = {"metrics": metrics.model_dump(mode="json"), **(extra or {})}

        with self._session_factory() as session:
            row = session.scalar(
                select(DailyOperationalMetricsModel).where(
                    DailyOperationalMetricsModel.session_date == session_date
                )
            )

            if row is None:
                row = DailyOperationalMetricsModel(session_date=session_date)
                session.add(row)

            row.computed_at = _required_db(metrics.computed_at)
            row.slippage_orders = metrics.slippage.orders
            row.slippage_mean_bps = metrics.slippage.mean_bps
            row.slippage_weighted_bps = metrics.slippage.weighted_bps
            row.slippage_total_cost = metrics.slippage.total_cost
            row.api_requests = metrics.latency.requests
            row.api_errors = metrics.latency.errors
            row.latency_mean_ms = metrics.latency.mean_ms
            row.latency_p50_ms = metrics.latency.p50_ms
            row.latency_p95_ms = metrics.latency.p95_ms
            row.orders_total = metrics.orders.total
            row.orders_open = metrics.orders.open
            row.fill_rate = metrics.orders.fill_rate
            row.reconciliation_consistent = reconciliation_consistent
            row.payload = payload
            session.commit()

    def daily_metrics(self, session_date: date) -> StoredDailyMetrics | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(DailyOperationalMetricsModel).where(
                    DailyOperationalMetricsModel.session_date == session_date
                )
            )

            if row is None:
                return None

            return StoredDailyMetrics(
                session_date=row.session_date,
                computed_at=row.computed_at.replace(tzinfo=UTC),
                slippage_orders=row.slippage_orders,
                slippage_mean_bps=row.slippage_mean_bps,
                api_requests=row.api_requests,
                api_errors=row.api_errors,
                latency_p50_ms=row.latency_p50_ms,
                latency_p95_ms=row.latency_p95_ms,
                orders_total=row.orders_total,
                orders_open=row.orders_open,
                fill_rate=row.fill_rate,
                reconciliation_consistent=row.reconciliation_consistent,
                payload=row.payload,
            )

    def alerts(
        self, since: datetime | None = None, code: str | None = None
    ) -> tuple[Alert, ...]:
        with self._session_factory() as session:
            query = select(OperationalAlertModel).order_by(
                OperationalAlertModel.created_at, OperationalAlertModel.id
            )

            if since is not None:
                query = query.where(OperationalAlertModel.created_at >= _to_db(since))

            if code is not None:
                query = query.where(OperationalAlertModel.code == code)

            return tuple(
                Alert(
                    level=AlertLevel(row.level),
                    code=row.code,
                    message=row.message,
                    timestamp=row.created_at.replace(tzinfo=UTC),
                    details=row.details,
                )
                for row in session.scalars(query)
            )


def _to_record(row: PendingProtectionModel) -> ProtectionRecord:
    return ProtectionRecord(
        entry_client_order_id=row.entry_client_order_id,
        symbol=row.symbol,
        session_date=row.session_date,
        entry_atr=row.entry_atr,
        stop_loss_atr_multiple=row.stop_loss_atr_multiple,
        take_profit_atr_multiple=row.take_profit_atr_multiple,
        signal_timestamp=_from_db(row.signal_timestamp),
        created_at=_from_db(row.created_at),
        resolved_at=_from_db(row.resolved_at),
        resolution=ProtectionResolution(row.resolution) if row.resolution else None,
        exit_client_order_id=row.exit_client_order_id,
    )


def _to_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(tzinfo=None)


def _required_db(value: datetime) -> datetime:
    converted = _to_db(value)
    assert converted is not None
    return converted


def _from_db(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None else None
