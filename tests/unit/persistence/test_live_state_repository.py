from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.persistence.models import Base, OperationalAlertModel
from algo_trading.persistence.repositories.live_state_repository import (
    DatabaseAlertSink,
    ProtectionRecord,
    ProtectionResolution,
    SqlProtectionStore,
    TelemetryStore,
)
from algo_trading.telemetry import AlertEmitter, AlertLevel, operational_metrics

NOW = datetime(2026, 10, 26, 13, 15, 0, 123456, tzinfo=UTC)
RECORD = ProtectionRecord(
    entry_client_order_id="algo-entry-AAPL-20261026-abc",
    symbol="AAPL",
    session_date=date(2026, 10, 26),
    entry_atr=Decimal("2.899660682765670834389925944"),
    stop_loss_atr_multiple=Decimal("2.0"),
    take_profit_atr_multiple=Decimal("3.0"),
    signal_timestamp=datetime(2026, 10, 23, tzinfo=UTC),
    created_at=NOW,
)


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def test_protection_store_round_trip_and_resolution(engine: Engine) -> None:
    store = SqlProtectionStore(sessionmaker(bind=engine), clock=lambda: NOW)

    store.add(RECORD)
    store.add(RECORD.model_copy(update={"symbol": "IGNORADO"}))  # idempotente

    (pending,) = store.pending()
    assert pending.symbol == "AAPL"
    assert pending.entry_atr == Decimal("2.899660682766")  # Numeric(28, 12)
    assert pending.created_at == NOW
    assert pending.signal_timestamp == datetime(2026, 10, 23, tzinfo=UTC)

    store.resolve(
        RECORD.entry_client_order_id, ProtectionResolution.PROTECTED, "algo-exit-1"
    )

    assert store.pending() == ()
    resolved = store.get(RECORD.entry_client_order_id)
    assert resolved is not None
    assert resolved.resolution is ProtectionResolution.PROTECTED
    assert resolved.exit_client_order_id == "algo-exit-1"
    assert resolved.resolved_at == NOW

    with pytest.raises(LookupError):
        store.resolve("missing", ProtectionResolution.NOT_FILLED)


def test_pending_protections_survive_a_new_store_instance(engine: Engine) -> None:
    factory = sessionmaker(bind=engine)
    SqlProtectionStore(factory).add(RECORD)

    restarted = SqlProtectionStore(factory)

    assert [r.entry_client_order_id for r in restarted.pending()] == [
        RECORD.entry_client_order_id
    ]


def test_alerts_are_stored_and_read_back(engine: Engine) -> None:
    factory = sessionmaker(bind=engine)
    emitter = AlertEmitter(DatabaseAlertSink(factory), clock=lambda: NOW)

    emitter.emit(AlertLevel.CRITICAL, "INVENTORY_DRIFT", "MSFT", {"difference": "3"})
    emitter.emit(AlertLevel.INFO, "PLAN_GENERATED", "Plan", {"orders": []})

    store = TelemetryStore(factory)
    drift = store.alerts(code="INVENTORY_DRIFT")
    assert [(a.level, a.code, a.details) for a in drift] == [
        (AlertLevel.CRITICAL, "INVENTORY_DRIFT", {"difference": "3"})
    ]
    assert drift[0].timestamp == NOW
    assert len(store.alerts(since=NOW)) == 2


def test_alert_sink_failures_never_raise(engine: Engine) -> None:
    def broken() -> Session:
        raise ConnectionError("MySQL caído")

    sink = DatabaseAlertSink(broken)
    AlertEmitter(sink, clock=lambda: NOW).emit(AlertLevel.INFO, "X", "mensaje")

    assert sink.failed == 1

    with Session(engine) as session:
        assert session.scalars(select(OperationalAlertModel)).all() == []


def test_daily_metrics_are_stored_and_replaced(engine: Engine) -> None:
    store = TelemetryStore(sessionmaker(bind=engine))
    metrics = operational_metrics([], [], clock=lambda: NOW)

    store.record_daily_metrics(date(2026, 10, 26), metrics, True, {"note": "primera"})
    store.record_daily_metrics(date(2026, 10, 26), metrics, False, {"note": "segunda"})

    stored = store.daily_metrics(date(2026, 10, 26))
    assert stored is not None
    assert stored.reconciliation_consistent is False
    assert stored.payload["note"] == "segunda"
    assert stored.payload["metrics"]["orders"]["total"] == 0
    assert stored.computed_at == NOW
    assert store.daily_metrics(date(2026, 10, 27)) is None
