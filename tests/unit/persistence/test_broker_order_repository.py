from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from algo_trading.execution.orders import (
    ExecutionSource,
    InvalidOrderTransitionError,
    OrderClass,
    OrderExecution,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from algo_trading.persistence.models import Base, BrokerOrderModel
from algo_trading.persistence.repositories.broker_order_repository import (
    BrokerOrderRepository,
    UnknownOrderError,
)

NOW = datetime(2024, 3, 5, 14, 31, tzinfo=UTC)
ENTRY_ID = "algo-entry-AAPL-20240305-abc"
REQUEST = OrderRequest(
    client_order_id=ENTRY_ID,
    symbol="AAPL",
    side=OrderSide.BUY,
    quantity=10,
    order_type=OrderType.LIMIT,
    time_in_force=TimeInForce.OPG,
    limit_price=Decimal("176.43"),
    theoretical_price=Decimal("175.10"),
    strategy_id="momentum_breakout_v1",
    signal_timestamp=datetime(2024, 3, 4, tzinfo=UTC),
)


def state(
    status: OrderStatus,
    filled: str = "0",
    average: str | None = None,
    client_order_id: str = ENTRY_ID,
    minute: int = 0,
    legs: tuple[OrderState, ...] = (),
    **extra: object,
) -> OrderState:
    return OrderState.model_validate(
        {
            "broker_order_id": f"broker-{client_order_id}",
            "client_order_id": client_order_id,
            "symbol": "AAPL",
            "side": OrderSide.BUY,
            "order_type": OrderType.LIMIT,
            "time_in_force": TimeInForce.OPG,
            "quantity": Decimal("10"),
            "filled_quantity": Decimal(filled),
            "filled_average_price": Decimal(average) if average else None,
            "limit_price": Decimal("176.43"),
            "status": status,
            "broker_status": status.value.lower(),
            "submitted_at": datetime(2024, 3, 5, 13, 0, tzinfo=UTC),
            "updated_at": datetime(2024, 3, 5, 14, 30 + minute, tzinfo=UTC),
            "filled_at": (
                datetime(2024, 3, 5, 14, 30 + minute, tzinfo=UTC)
                if status is OrderStatus.FILLED
                else None
            ),
            "legs": legs,
            **extra,
        }
    )


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    with Session(engine) as database_session:
        yield database_session

    engine.dispose()


@pytest.fixture
def repository(session: Session) -> BrokerOrderRepository:
    ticks = iter(NOW + timedelta(seconds=second) for second in range(1000))
    return BrokerOrderRepository(session, clock=lambda: next(ticks))


def test_record_submission_keeps_request_context(
    repository: BrokerOrderRepository,
) -> None:
    result = repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))

    assert result.created
    assert result.previous_status is None
    assert result.status is OrderStatus.SUBMITTED
    record = repository.get_record(ENTRY_ID)
    assert record is not None
    assert record.theoretical_price == Decimal("175.10")
    assert record.strategy_id == "momentum_breakout_v1"
    assert record.signal_timestamp == datetime(2024, 3, 4, tzinfo=UTC)
    assert record.created_at == NOW
    assert record.state == state(OrderStatus.SUBMITTED)


def test_submission_and_state_must_match(repository: BrokerOrderRepository) -> None:
    with pytest.raises(ValueError, match="distintas"):
        repository.record_submission(
            REQUEST, state(OrderStatus.SUBMITTED, client_order_id="other")
        )


def test_partial_and_total_fills_are_deduced_from_order_updates(
    repository: BrokerOrderRepository,
) -> None:
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))

    first = repository.apply_state(state(OrderStatus.PARTIALLY_FILLED, "4", "176.10"))
    second = repository.apply_state(
        state(OrderStatus.PARTIALLY_FILLED, "7", "176.13", minute=1)
    )
    final = repository.apply_state(state(OrderStatus.FILLED, "10", "176.15", minute=2))

    assert first.status_changed and not second.status_changed and final.status_changed
    executions = repository.get_executions(ENTRY_ID)
    assert [(e.quantity, e.cumulative_quantity) for e in executions] == [
        (Decimal("4"), Decimal("4")),
        (Decimal("3"), Decimal("7")),
        (Decimal("3"), Decimal("10")),
    ]
    # (176.13·7 - 176.10·4) / 3 y (176.15·10 - 176.13·7) / 3
    assert [e.price for e in executions] == [
        Decimal("176.10"),
        Decimal("176.17"),
        Decimal("176.19666667"),
    ]
    assert [e.execution_id for e in executions] == [
        f"{ENTRY_ID}:4",
        f"{ENTRY_ID}:7",
        f"{ENTRY_ID}:10",
    ]
    assert executions[-1].executed_at == datetime(2024, 3, 5, 14, 32, tzinfo=UTC)
    assert all(e.source is ExecutionSource.ORDER_UPDATE for e in executions)
    assert all(e.commission == 0 for e in executions)

    stored = repository.get(ENTRY_ID)
    assert stored is not None
    assert stored.status is OrderStatus.FILLED
    assert stored.filled_average_price == Decimal("176.15")
    assert repository.list_open() == ()


def test_repeated_state_is_idempotent(repository: BrokerOrderRepository) -> None:
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))
    repository.apply_state(state(OrderStatus.FILLED, "10", "176.15"))

    again = repository.apply_state(state(OrderStatus.FILLED, "10", "176.15"))

    assert not again.created
    assert not again.status_changed
    assert again.executions == ()
    assert len(repository.get_executions(ENTRY_ID)) == 1


def test_fill_in_a_single_update_from_submitted(
    repository: BrokerOrderRepository,
) -> None:
    result = repository.apply_state(state(OrderStatus.FILLED, "10", "176.15"))

    assert result.created
    (execution,) = result.executions
    assert (execution.quantity, execution.price) == (Decimal("10"), Decimal("176.15"))


def test_partial_fill_then_cancel_keeps_executed_quantity(
    repository: BrokerOrderRepository,
) -> None:
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))
    repository.apply_state(state(OrderStatus.PARTIALLY_FILLED, "3", "176.20"))

    repository.apply_state(state(OrderStatus.CANCELED, "3", "176.20", minute=5))

    stored = repository.get(ENTRY_ID)
    assert stored is not None
    assert stored.status is OrderStatus.CANCELED
    assert stored.filled_quantity == Decimal("3")
    assert len(repository.get_executions(ENTRY_ID)) == 1


@pytest.mark.parametrize(
    ("final_status", "broker_status"),
    [(OrderStatus.EXPIRED, "expired"), (OrderStatus.CANCELED, "canceled")],
)
def test_unfilled_opening_order_is_closed_without_executions(
    repository: BrokerOrderRepository, final_status: OrderStatus, broker_status: str
) -> None:
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))

    repository.apply_state(state(final_status, broker_status=broker_status))

    assert repository.get_executions(ENTRY_ID) == ()
    assert repository.list_open() == ()


def test_invalid_transitions_are_rejected_without_changes(
    repository: BrokerOrderRepository,
) -> None:
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))
    repository.apply_state(state(OrderStatus.FILLED, "10", "176.15"))

    with pytest.raises(InvalidOrderTransitionError):
        repository.apply_state(state(OrderStatus.SUBMITTED))

    with pytest.raises(InvalidOrderTransitionError, match="no puede bajar"):
        repository.apply_state(state(OrderStatus.FILLED, "8", "176.15"))

    stored = repository.get(ENTRY_ID)
    assert stored is not None
    assert (stored.status, stored.filled_quantity) == (
        OrderStatus.FILLED,
        Decimal("10"),
    )


def test_fill_without_average_price_is_an_error(
    repository: BrokerOrderRepository,
) -> None:
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))

    with pytest.raises(ValueError, match="sin precio medio"):
        repository.apply_state(state(OrderStatus.PARTIALLY_FILLED, "4"))


def test_oco_legs_are_stored_as_child_orders(
    repository: BrokerOrderRepository, session: Session
) -> None:
    exit_id = "algo-exit-AAPL-20240305-abc"
    stop_leg = state(
        OrderStatus.SUBMITTED,
        client_order_id="leg-stop",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        time_in_force=TimeInForce.GTC,
        limit_price=None,
        stop_price=Decimal("170.65"),
    )
    request = OrderRequest(
        client_order_id=exit_id,
        symbol="AAPL",
        side=OrderSide.SELL,
        quantity=10,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OCO,
        take_profit_limit_price=Decimal("184.04"),
        stop_loss_stop_price=Decimal("170.65"),
    )
    oco = state(
        OrderStatus.SUBMITTED,
        client_order_id=exit_id,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.OCO,
        limit_price=Decimal("184.04"),
        legs=(stop_leg,),
    )

    repository.record_submission(request, oco)

    record = repository.get_record(exit_id)
    assert record is not None
    assert record.take_profit_limit_price == Decimal("184.04")
    assert record.stop_loss_stop_price == Decimal("170.65")
    assert record.state.legs == (stop_leg,)
    leg = repository.get_record("leg-stop")
    assert leg is not None
    assert leg.parent_client_order_id == exit_id
    assert [order.client_order_id for order in repository.list_open()] == [
        exit_id,
        "leg-stop",
    ]

    # La pata de stop se ejecuta: la OCO cancela la otra.
    repository.apply_state(
        oco.model_copy(
            update={
                "status": OrderStatus.CANCELED,
                "broker_status": "canceled",
                "legs": (
                    stop_leg.model_copy(
                        update={
                            "status": OrderStatus.FILLED,
                            "broker_status": "filled",
                            "filled_quantity": Decimal("10"),
                            "filled_average_price": Decimal("170.60"),
                        }
                    ),
                ),
            }
        )
    )

    assert repository.list_open() == ()
    (execution,) = repository.get_executions("leg-stop")
    assert execution.price == Decimal("170.60")


def test_broker_activity_executions_are_idempotent(
    repository: BrokerOrderRepository,
) -> None:
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))
    activity = OrderExecution(
        execution_id="20240305143000000::fill-1",
        client_order_id=ENTRY_ID,
        executed_at=datetime(2024, 3, 5, 14, 30, tzinfo=UTC),
        quantity=Decimal("10"),
        price=Decimal("176.15"),
        cumulative_quantity=Decimal("10"),
        commission=Decimal("0.01"),
        source=ExecutionSource.BROKER_ACTIVITY,
    )

    assert repository.record_execution(activity)
    assert not repository.record_execution(activity)
    repository.apply_state(
        state(OrderStatus.FILLED, "10", "176.15"), deduce_executions=False
    )

    assert repository.get_executions(ENTRY_ID) == (activity,)

    with pytest.raises(UnknownOrderError):
        repository.record_execution(
            activity.model_copy(update={"client_order_id": "x"})
        )


def test_timestamps_are_stored_naive_utc(
    repository: BrokerOrderRepository, session: Session
) -> None:
    repository.record_submission(REQUEST, state(OrderStatus.SUBMITTED))

    row = session.scalar(select(BrokerOrderModel))
    assert row is not None
    assert row.submitted_at == datetime(2024, 3, 5, 13, 0)
    assert row.submitted_at.tzinfo is None
    assert row.last_synced_at == NOW.replace(tzinfo=None)
