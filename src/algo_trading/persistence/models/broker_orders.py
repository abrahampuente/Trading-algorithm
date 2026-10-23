from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import Mapped, mapped_column

from algo_trading.persistence.models.base import Base

MONEY = Numeric(20, 8)
QUANTITY = Numeric(20, 8)
# Microsegundos en MySQL (DATETIME(6)): las latencias de órdenes y fills
# (Hito 4) se miden por debajo del segundo.
TIMESTAMP = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


class BrokerOrderModel(Base):
    """
    Orden enviada al broker (paper) y su último estado conocido.

    `status` es el estado normalizado de `OrderStatus`; `broker_status`, el
    original del broker. Las patas de una OCO son filas propias con
    `parent_client_order_id`. Timestamps en UTC sin zona.
    """

    __tablename__ = "broker_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_client_order_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    time_in_force: Mapped[str] = mapped_column(String(8), nullable=False)
    order_class: Mapped[str] = mapped_column(String(8), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    take_profit_limit_price: Mapped[Decimal | None] = mapped_column(
        MONEY, nullable=True
    )
    stop_loss_stop_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    theoretical_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    signal_timestamp: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False)
    broker_status: Mapped[str] = mapped_column(String(32), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    filled_average_price: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    filled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    broker_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    last_synced_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_broker_orders_client_order_id"),
        UniqueConstraint("broker_order_id", name="uq_broker_orders_broker_order_id"),
        Index("ix_broker_orders_status", "status"),
        Index("ix_broker_orders_symbol_created_at", "symbol", "created_at"),
        Index("ix_broker_orders_parent_client_order_id", "parent_client_order_id"),
    )


class OrderExecutionModel(Base):
    """
    Ejecución (fill parcial o total) de una orden.

    `execution_id` es único: el id de actividad del broker o, si la ejecución
    se deduce del estado de la orden, `<client_order_id>:<cantidad acumulada>`.
    """

    __tablename__ = "order_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("broker_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    execution_id: Mapped[str] = mapped_column(String(160), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    price: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    cumulative_quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    commission: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_order_executions_execution_id"),
        Index("ix_order_executions_order_id_executed_at", "order_id", "executed_at"),
    )
