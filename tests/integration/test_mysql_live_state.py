"""Estado operativo contra el esquema real de MySQL (migración c2d3e4f5a6b7)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from algo_trading.persistence.database import SessionFactory
from algo_trading.persistence.models import (
    DailyOperationalMetricsModel,
    OperationalAlertModel,
    PendingProtectionModel,
)
from algo_trading.persistence.repositories.live_state_repository import (
    DatabaseAlertSink,
    ProtectionRecord,
    ProtectionResolution,
    SqlProtectionStore,
    TelemetryStore,
)
from algo_trading.telemetry import AlertEmitter, AlertLevel, operational_metrics

PREFIX = "it-live-"
TEST_DATE = date(2099, 1, 2)  # fuera de cualquier sesión real
NOW = datetime(2099, 1, 2, 13, 15, 0, 654321, tzinfo=UTC)


def cleanup(session: Session) -> None:
    session.execute(
        delete(PendingProtectionModel).where(
            PendingProtectionModel.entry_client_order_id.like(f"{PREFIX}%")
        )
    )
    session.execute(
        delete(OperationalAlertModel).where(
            OperationalAlertModel.code.like("IT_LIVE_%")
        )
    )
    session.execute(
        delete(DailyOperationalMetricsModel).where(
            DailyOperationalMetricsModel.session_date == TEST_DATE
        )
    )
    session.commit()


@pytest.fixture
def session() -> Iterator[Session]:
    with SessionFactory() as database_session:
        try:
            database_session.execute(text("SELECT 1"))
        except OperationalError as error:
            pytest.skip(f"MySQL no disponible: {error.orig}")

        cleanup(database_session)
        yield database_session
        database_session.rollback()
        cleanup(database_session)


def test_live_state_round_trip_in_mysql(session: Session) -> None:
    store = SqlProtectionStore(SessionFactory, clock=lambda: NOW)
    record = ProtectionRecord(
        entry_client_order_id=f"{PREFIX}entry-AAPL",
        symbol="AAPL",
        session_date=TEST_DATE,
        entry_atr=Decimal("2.899660682765670834"),
        stop_loss_atr_multiple=Decimal("2.0"),
        take_profit_atr_multiple=Decimal("3.0"),
        created_at=NOW,
    )

    store.add(record)
    store.add(record)  # idempotente: la clave única no admite duplicados

    pending = [
        r
        for r in store.pending()
        if r.entry_client_order_id == record.entry_client_order_id
    ]
    assert len(pending) == 1
    assert pending[0].created_at == NOW  # DATETIME(6)
    assert pending[0].entry_atr == Decimal("2.899660682766")

    store.resolve(record.entry_client_order_id, ProtectionResolution.NOT_FILLED)
    resolved = store.get(record.entry_client_order_id)
    assert resolved is not None
    assert resolved.resolution is ProtectionResolution.NOT_FILLED

    AlertEmitter(DatabaseAlertSink(SessionFactory), clock=lambda: NOW).emit(
        AlertLevel.WARNING, "IT_LIVE_TEST", "prueba de integración", {"n": 1}
    )
    telemetry = TelemetryStore(SessionFactory)
    (alert,) = telemetry.alerts(code="IT_LIVE_TEST")
    assert alert.timestamp == NOW
    assert alert.details == {"n": 1}

    telemetry.record_daily_metrics(
        TEST_DATE, operational_metrics([], [], clock=lambda: NOW), True
    )
    stored = telemetry.daily_metrics(TEST_DATE)
    assert stored is not None
    assert stored.computed_at == NOW
    assert stored.reconciliation_consistent is True
