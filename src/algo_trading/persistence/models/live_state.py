"""Estado operativo del paper trading (Hito 4): protecciones, alertas y métricas."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from algo_trading.persistence.models.base import Base
from algo_trading.persistence.models.broker_orders import MONEY, TIMESTAMP

ATR = Numeric(28, 12)
MULTIPLE = Numeric(10, 4)
BPS = Numeric(20, 4)


class PendingProtectionModel(Base):
    """
    Entrada del plan a la espera de ejecución para enviar su OCO SL / TP.

    Guarda lo necesario para recalcular los niveles sobre el precio
    ejecutado (ATR congelado de la señal y multiplicadores). `resolution`
    nulo = pendiente; `PROTECTED`, `NOT_FILLED` o `NOT_SUBMITTED` al cerrarse.
    """

    __tablename__ = "pending_protections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_client_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    entry_atr: Mapped[Decimal] = mapped_column(ATR, nullable=False)
    stop_loss_atr_multiple: Mapped[Decimal] = mapped_column(MULTIPLE, nullable=False)
    take_profit_atr_multiple: Mapped[Decimal] = mapped_column(MULTIPLE, nullable=False)
    signal_timestamp: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(20), nullable=True)
    exit_client_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "entry_client_order_id", name="uq_pending_protections_entry_client_order_id"
        ),
        Index("ix_pending_protections_resolution", "resolution"),
    )


class OperationalAlertModel(Base):
    """Alerta operativa emitida por el daemon (una fila por alerta)."""

    __tablename__ = "operational_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    level: Mapped[str] = mapped_column(String(10), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_operational_alerts_created_at", "created_at"),
        Index("ix_operational_alerts_code_created_at", "code", "created_at"),
    )


class DailyOperationalMetricsModel(Base):
    """Métricas operativas de una sesión (una fila por sesión; se reemplaza)."""

    __tablename__ = "daily_operational_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    slippage_orders: Mapped[int] = mapped_column(Integer, nullable=False)
    slippage_mean_bps: Mapped[Decimal | None] = mapped_column(BPS, nullable=True)
    slippage_weighted_bps: Mapped[Decimal | None] = mapped_column(BPS, nullable=True)
    slippage_total_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    api_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    api_errors: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_mean_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_p50_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_p95_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    orders_total: Mapped[int] = mapped_column(Integer, nullable=False)
    orders_open: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    reconciliation_consistent: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "session_date", name="uq_daily_operational_metrics_session_date"
        ),
    )
