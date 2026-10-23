"""Órdenes y ejecuciones contra el esquema real de MySQL (migración b7c8d9e0f1a2)."""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from algo_trading.execution.orders import (
    OrderRequest,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from algo_trading.persistence.database import SessionFactory
from algo_trading.persistence.models import BrokerOrderModel, OrderExecutionModel
from algo_trading.persistence.repositories.broker_order_repository import (
    BrokerOrderRepository,
)

PREFIX = "it-broker-"
CLIENT_ID = f"{PREFIX}entry-AAPL"


def cleanup(session: Session) -> None:
    session.execute(
        delete(BrokerOrderModel).where(
            BrokerOrderModel.client_order_id.like(f"{PREFIX}%")
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


def state(
    status: OrderStatus, filled: str = "0", average: str | None = None
) -> OrderState:
    return OrderState(
        broker_order_id=f"{PREFIX}broker-1",
        client_order_id=CLIENT_ID,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.OPG,
        quantity=Decimal("10"),
        filled_quantity=Decimal(filled),
        filled_average_price=Decimal(average) if average else None,
        limit_price=Decimal("176.43"),
        status=status,
        broker_status=status.value.lower(),
        updated_at=datetime(2024, 3, 5, 14, 30, 0, 123456, tzinfo=UTC),
    )


REQUEST = OrderRequest(
    client_order_id=CLIENT_ID,
    symbol="AAPL",
    side=OrderSide.BUY,
    quantity=10,
    order_type=OrderType.LIMIT,
    time_in_force=TimeInForce.OPG,
    limit_price=Decimal("176.43"),
    theoretical_price=Decimal("175.10"),
)


def test_order_lifecycle_is_persisted_in_mysql(session: Session) -> None:
    repository = BrokerOrderRepository(session)
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))
    repository.apply_state(state(OrderStatus.PARTIALLY_FILLED, "4", "176.10"))
    repository.apply_state(state(OrderStatus.FILLED, "10", "176.15"))
    session.commit()

    reloaded = BrokerOrderRepository(session).get_record(CLIENT_ID)
    assert reloaded is not None
    assert reloaded.state.status is OrderStatus.FILLED
    assert reloaded.state.filled_quantity == Decimal("10.00000000")
    assert reloaded.theoretical_price == Decimal("175.10000000")
    # DATETIME(6): se conservan los microsegundos (latencias del Hito 4).
    assert reloaded.state.updated_at == datetime(
        2024, 3, 5, 14, 30, 0, 123456, tzinfo=UTC
    )
    assert [e.price for e in repository.get_executions(CLIENT_ID)] == [
        Decimal("176.10000000"),
        Decimal("176.18333333"),
    ]


def test_client_order_id_is_unique(session: Session) -> None:
    BrokerOrderRepository(session).record_submission(
        REQUEST, state(OrderStatus.SUBMITTED)
    )
    session.commit()
    row = session.scalar(
        select(BrokerOrderModel).where(BrokerOrderModel.client_order_id == CLIENT_ID)
    )
    assert row is not None

    duplicate = BrokerOrderModel(
        **{
            column.name: getattr(row, column.name)
            for column in BrokerOrderModel.__table__.columns
            if column.name not in ("id", "broker_order_id")
        },
        broker_order_id=f"{PREFIX}broker-2",
    )
    session.add(duplicate)

    with pytest.raises(IntegrityError):
        session.flush()


def test_deleting_an_order_cascades_to_its_executions(session: Session) -> None:
    repository = BrokerOrderRepository(session)
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))
    repository.apply_state(state(OrderStatus.FILLED, "10", "176.15"))
    session.commit()

    cleanup(session)

    remaining = session.scalar(
        select(func.count())
        .select_from(OrderExecutionModel)
        .where(OrderExecutionModel.execution_id.like(f"{PREFIX}%"))
    )
    assert remaining == 0
