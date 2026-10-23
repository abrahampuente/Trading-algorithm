"""
Órdenes para el broker de paper trading (Hito 3).

Contratos independientes del broker: la solicitud (`OrderRequest`), el estado
que informa el broker (`OrderState`), los estados normalizados
(`OrderStatus`) con su máquina de estados y un registro local de órdenes
(`OrderBook`) que valida cada transición.

Precios: Alpaca exige incrementos de 0,01 para precios >= 1 y de 0,0001 por
debajo. `round_to_tick` redondea siempre en la dirección prudente que se le
indique (ver `OrderPlanner`).
"""

from collections.abc import Iterable
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

CENT_TICK = Decimal("0.01")
SUB_DOLLAR_TICK = Decimal("0.0001")
MAX_CLIENT_ORDER_ID_LENGTH = 128


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(StrEnum):
    DAY = "day"
    GTC = "gtc"
    OPG = "opg"  # sólo en la subasta de apertura


class OrderClass(StrEnum):
    SIMPLE = "simple"
    OCO = "oco"  # salida: TP límite y SL stop, la ejecución de uno cancela el otro


class OrderStatus(StrEnum):
    """Estados normalizados; `broker_status` conserva el estado original."""

    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    REPLACED = "REPLACED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
        OrderStatus.REPLACED,
    }
)

# Estados de Alpaca (`order.status`) a estados normalizados. Los transitorios
# (pendiente de alta, de cancelación o de sustitución, `done_for_day`) siguen
# vivos en el broker y cuentan como SUBMITTED.
ALPACA_STATUSES = {
    "pending_new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "new": OrderStatus.SUBMITTED,
    "pending_cancel": OrderStatus.SUBMITTED,
    "pending_replace": OrderStatus.SUBMITTED,
    "done_for_day": OrderStatus.SUBMITTED,
    "calculated": OrderStatus.SUBMITTED,
    "stopped": OrderStatus.SUBMITTED,
    "suspended": OrderStatus.SUBMITTED,
    "held": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
    "replaced": OrderStatus.REPLACED,
}


def normalize_alpaca_status(status: str) -> OrderStatus:
    try:
        return ALPACA_STATUSES[status]
    except KeyError:
        raise ValueError(f"Estado de orden de Alpaca desconocido: {status}") from None


class InvalidOrderTransitionError(ValueError):
    """El broker informó un cambio de estado imposible (p. ej. FILLED → SUBMITTED)."""


def validate_transition(previous: OrderStatus, current: OrderStatus) -> None:
    """
    Transiciones permitidas:

    - SUBMITTED → cualquier estado.
    - PARTIALLY_FILLED → PARTIALLY_FILLED, FILLED o un cierre (CANCELED,
      EXPIRED, REPLACED); una cancelación deja la posición parcial.
    - Un estado final sólo se repite a sí mismo.
    """
    if previous is current or previous is OrderStatus.SUBMITTED:
        return

    if previous is OrderStatus.PARTIALLY_FILLED and current in {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REPLACED,
    }:
        return

    raise InvalidOrderTransitionError(f"Transición inválida: {previous} → {current}")


def round_to_tick(price: Decimal, rounding: str) -> Decimal:
    """Redondea al tick de Alpaca (`ROUND_FLOOR` o `ROUND_CEILING`)."""
    if rounding not in (ROUND_FLOOR, ROUND_CEILING):
        raise ValueError("rounding debe ser ROUND_FLOOR o ROUND_CEILING")

    if price <= 0:
        raise ValueError("El precio debe ser positivo")

    tick = CENT_TICK if price >= 1 else SUB_DOLLAR_TICK
    return price.quantize(tick, rounding=rounding)


class OrderRequest(BaseModel):
    """
    Orden a enviar. `client_order_id` es determinista (idempotencia): reenviar
    el mismo plan no duplica órdenes. `theoretical_price` es el precio que
    usaría el backtest y sirve para medir el slippage real.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str = Field(min_length=1, max_length=MAX_CLIENT_ORDER_ID_LENGTH)
    symbol: str = Field(min_length=1, max_length=32)
    side: OrderSide
    quantity: int = Field(ge=1)
    order_type: OrderType
    time_in_force: TimeInForce
    order_class: OrderClass = OrderClass.SIMPLE
    limit_price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    take_profit_limit_price: Decimal | None = Field(default=None, gt=0)
    stop_loss_stop_price: Decimal | None = Field(default=None, gt=0)
    theoretical_price: Decimal | None = Field(default=None, gt=0)
    strategy_id: str | None = None
    signal_timestamp: datetime | None = None

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.order_class is OrderClass.OCO:
            self._validate_oco()
            return self

        if self.take_profit_limit_price or self.stop_loss_stop_price:
            raise ValueError("Las patas TP / SL sólo existen en órdenes OCO")

        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("Una orden limitada necesita limit_price")

        if self.order_type is OrderType.STOP and self.stop_price is None:
            raise ValueError("Una orden stop necesita stop_price")

        if self.order_type is OrderType.MARKET and (
            self.limit_price is not None or self.stop_price is not None
        ):
            raise ValueError("Una orden de mercado no lleva precios")

        if self.time_in_force is TimeInForce.OPG and self.order_type is OrderType.STOP:
            raise ValueError("OPG sólo admite órdenes de mercado o limitadas")

        return self

    def _validate_oco(self) -> None:
        if self.side is not OrderSide.SELL or self.order_type is not OrderType.LIMIT:
            raise ValueError("La salida OCO es una venta limitada (TP) con stop (SL)")

        if self.take_profit_limit_price is None or self.stop_loss_stop_price is None:
            raise ValueError("Una OCO necesita take_profit y stop_loss")

        if self.stop_loss_stop_price >= self.take_profit_limit_price:
            raise ValueError("El stop loss debe quedar por debajo del take profit")

        if self.time_in_force not in (TimeInForce.DAY, TimeInForce.GTC):
            raise ValueError("Alpaca sólo admite OCO con time_in_force day o gtc")

        if self.limit_price is not None or self.stop_price is not None:
            raise ValueError("En una OCO los precios van en las patas TP / SL")


class OrderState(BaseModel):
    """Estado de una orden según el broker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    time_in_force: TimeInForce
    order_class: OrderClass = OrderClass.SIMPLE
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    filled_average_price: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    status: OrderStatus
    broker_status: str
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    updated_at: datetime | None = None
    legs: tuple["OrderState", ...] = ()

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity


class OrderEvent(BaseModel):
    """Cambio de estado observado de una orden."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_order_id: str
    previous_status: OrderStatus | None
    status: OrderStatus
    filled_quantity: Decimal
    observed_at: datetime | None


class OrderBook:
    """
    Registro local de órdenes: guarda el último estado de cada una y valida
    cada actualización con `validate_transition`. Una transición inválida se
    lanza sin modificar el estado guardado. Las patas de una OCO se registran
    como órdenes propias.
    """

    def __init__(self) -> None:
        self._orders: dict[str, OrderState] = {}
        self._events: list[OrderEvent] = []

    @property
    def events(self) -> tuple[OrderEvent, ...]:
        return tuple(self._events)

    def get(self, client_order_id: str) -> OrderState | None:
        return self._orders.get(client_order_id)

    def open_orders(self) -> tuple[OrderState, ...]:
        return tuple(
            order
            for _, order in sorted(self._orders.items())
            if not order.status.is_terminal
        )

    def apply(self, state: OrderState) -> OrderEvent | None:
        """Registra el estado; devuelve el evento si cambió algo."""
        for leg in state.legs:
            self.apply(leg)

        previous = self._orders.get(state.client_order_id)

        if previous is not None:
            validate_transition(previous.status, state.status)

            if state.filled_quantity < previous.filled_quantity:
                raise InvalidOrderTransitionError(
                    f"{state.client_order_id}: la cantidad ejecutada no puede bajar"
                )

        self._orders[state.client_order_id] = state

        if previous is not None and (
            previous.status is state.status
            and previous.filled_quantity == state.filled_quantity
        ):
            return None

        event = OrderEvent(
            client_order_id=state.client_order_id,
            previous_status=previous.status if previous is not None else None,
            status=state.status,
            filled_quantity=state.filled_quantity,
            observed_at=state.updated_at,
        )
        self._events.append(event)
        return event

    def apply_all(self, states: Iterable[OrderState]) -> tuple[OrderEvent, ...]:
        return tuple(
            event for state in states if (event := self.apply(state)) is not None
        )
