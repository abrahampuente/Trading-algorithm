from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import pytest

from algo_trading.execution.orders import (
    ALPACA_STATUSES,
    InvalidOrderTransitionError,
    OrderBook,
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    TimeInForce,
    normalize_alpaca_status,
    round_to_tick,
    validate_transition,
)


def state(
    status: OrderStatus,
    filled: str = "0",
    client_order_id: str = "algo-entry-AAPL",
    legs: tuple[OrderState, ...] = (),
) -> OrderState:
    return OrderState(
        broker_order_id=f"broker-{client_order_id}",
        client_order_id=client_order_id,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.OPG,
        quantity=Decimal("10"),
        filled_quantity=Decimal(filled),
        status=status,
        broker_status=status.value.lower(),
        updated_at=datetime(2024, 3, 4, 14, 30, tzinfo=UTC),
        legs=legs,
    )


@pytest.mark.parametrize(
    ("alpaca", "expected"),
    [
        ("new", OrderStatus.SUBMITTED),
        ("accepted", OrderStatus.SUBMITTED),
        ("pending_new", OrderStatus.SUBMITTED),
        ("pending_cancel", OrderStatus.SUBMITTED),
        ("done_for_day", OrderStatus.SUBMITTED),
        ("partially_filled", OrderStatus.PARTIALLY_FILLED),
        ("filled", OrderStatus.FILLED),
        ("canceled", OrderStatus.CANCELED),
        ("expired", OrderStatus.EXPIRED),
        ("rejected", OrderStatus.REJECTED),
        ("replaced", OrderStatus.REPLACED),
    ],
)
def test_alpaca_statuses_are_normalized(alpaca: str, expected: OrderStatus) -> None:
    assert normalize_alpaca_status(alpaca) is expected


def test_unknown_alpaca_status_is_an_error() -> None:
    with pytest.raises(ValueError, match="desconocido"):
        normalize_alpaca_status("teleported")

    assert all(isinstance(status, OrderStatus) for status in ALPACA_STATUSES.values())


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (OrderStatus.SUBMITTED, OrderStatus.SUBMITTED),
        (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.SUBMITTED, OrderStatus.FILLED),
        (OrderStatus.SUBMITTED, OrderStatus.CANCELED),
        (OrderStatus.SUBMITTED, OrderStatus.EXPIRED),
        (OrderStatus.SUBMITTED, OrderStatus.REJECTED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.EXPIRED),
        (OrderStatus.FILLED, OrderStatus.FILLED),
    ],
)
def test_valid_transitions(previous: OrderStatus, current: OrderStatus) -> None:
    validate_transition(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (OrderStatus.FILLED, OrderStatus.SUBMITTED),
        (OrderStatus.FILLED, OrderStatus.CANCELED),
        (OrderStatus.CANCELED, OrderStatus.FILLED),
        (OrderStatus.EXPIRED, OrderStatus.PARTIALLY_FILLED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.SUBMITTED),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.REJECTED),
    ],
)
def test_invalid_transitions(previous: OrderStatus, current: OrderStatus) -> None:
    with pytest.raises(InvalidOrderTransitionError):
        validate_transition(previous, current)


def test_order_book_records_lifecycle_events() -> None:
    book = OrderBook()

    events = book.apply_all(
        [
            state(OrderStatus.SUBMITTED),
            state(OrderStatus.SUBMITTED),
            state(OrderStatus.PARTIALLY_FILLED, "4"),
            state(OrderStatus.PARTIALLY_FILLED, "7"),
            state(OrderStatus.FILLED, "10"),
        ]
    )

    assert [(e.previous_status, e.status, e.filled_quantity) for e in events] == [
        (None, OrderStatus.SUBMITTED, Decimal("0")),
        (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED, Decimal("4")),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.PARTIALLY_FILLED, Decimal("7")),
        (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, Decimal("10")),
    ]
    assert book.open_orders() == ()
    assert book.get("algo-entry-AAPL") == state(OrderStatus.FILLED, "10")


def test_partial_fill_then_cancel_keeps_partial_quantity() -> None:
    book = OrderBook()
    book.apply_all(
        [state(OrderStatus.PARTIALLY_FILLED, "3"), state(OrderStatus.CANCELED, "3")]
    )

    canceled = book.get("algo-entry-AAPL")
    assert canceled is not None
    assert canceled.filled_quantity == Decimal("3")
    assert canceled.remaining_quantity == Decimal("7")


def test_order_book_rejects_impossible_updates_without_changing_state() -> None:
    book = OrderBook()
    book.apply(state(OrderStatus.FILLED, "10"))

    with pytest.raises(InvalidOrderTransitionError):
        book.apply(state(OrderStatus.SUBMITTED))

    with pytest.raises(InvalidOrderTransitionError, match="no puede bajar"):
        book.apply(state(OrderStatus.FILLED, "5"))

    assert book.get("algo-entry-AAPL") == state(OrderStatus.FILLED, "10")


def test_oco_legs_are_tracked_as_orders() -> None:
    take_profit = state(OrderStatus.SUBMITTED, client_order_id="tp-leg")
    stop_loss = state(OrderStatus.SUBMITTED, client_order_id="sl-leg")
    book = OrderBook()

    book.apply(
        state(
            OrderStatus.SUBMITTED, client_order_id="oco", legs=(take_profit, stop_loss)
        )
    )

    assert {order.client_order_id for order in book.open_orders()} == {
        "oco",
        "tp-leg",
        "sl-leg",
    }


@pytest.mark.parametrize(
    ("price", "rounding", "expected"),
    [
        ("101.2349", ROUND_FLOOR, "101.23"),
        ("101.2301", ROUND_CEILING, "101.24"),
        ("101.23", ROUND_CEILING, "101.23"),
        ("0.123456", ROUND_FLOOR, "0.1234"),
        ("0.123401", ROUND_CEILING, "0.1235"),
    ],
)
def test_round_to_tick(price: str, rounding: str, expected: str) -> None:
    assert round_to_tick(Decimal(price), rounding) == Decimal(expected)


def test_round_to_tick_rejects_other_modes_and_non_positive_prices() -> None:
    with pytest.raises(ValueError, match="ROUND_FLOOR"):
        round_to_tick(Decimal("1"), "ROUND_HALF_EVEN")

    with pytest.raises(ValueError, match="positivo"):
        round_to_tick(Decimal("0"), ROUND_FLOOR)


BASE = {
    "client_order_id": "algo-1",
    "symbol": "AAPL",
    "quantity": 10,
}


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"side": "buy", "order_type": "limit", "time_in_force": "opg"}, "limit_price"),
        ({"side": "sell", "order_type": "stop", "time_in_force": "day"}, "stop_price"),
        (
            {
                "side": "buy",
                "order_type": "market",
                "time_in_force": "opg",
                "limit_price": "10",
            },
            "no lleva precios",
        ),
        (
            {
                "side": "sell",
                "order_type": "stop",
                "time_in_force": "opg",
                "stop_price": "10",
            },
            "OPG",
        ),
        (
            {
                "side": "sell",
                "order_type": "limit",
                "time_in_force": "gtc",
                "order_class": "oco",
                "take_profit_limit_price": "110",
            },
            "take_profit y stop_loss",
        ),
        (
            {
                "side": "sell",
                "order_type": "limit",
                "time_in_force": "opg",
                "order_class": "oco",
                "take_profit_limit_price": "110",
                "stop_loss_stop_price": "95",
            },
            "day o gtc",
        ),
        (
            {
                "side": "sell",
                "order_type": "limit",
                "time_in_force": "gtc",
                "order_class": "oco",
                "take_profit_limit_price": "95",
                "stop_loss_stop_price": "110",
            },
            "por debajo",
        ),
        (
            {
                "side": "buy",
                "order_type": "limit",
                "time_in_force": "gtc",
                "order_class": "oco",
                "take_profit_limit_price": "110",
                "stop_loss_stop_price": "95",
            },
            "venta limitada",
        ),
        (
            {
                "side": "sell",
                "order_type": "limit",
                "time_in_force": "day",
                "limit_price": "110",
                "take_profit_limit_price": "110",
            },
            "sólo existen en órdenes OCO",
        ),
    ],
)
def test_invalid_order_requests(fields: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        OrderRequest.model_validate({**BASE, **fields})


def test_valid_oco_request() -> None:
    request = OrderRequest.model_validate(
        {
            **BASE,
            "side": "sell",
            "order_type": "limit",
            "time_in_force": "gtc",
            "order_class": "oco",
            "take_profit_limit_price": "110",
            "stop_loss_stop_price": "95",
        }
    )

    assert request.order_class is OrderClass.OCO
