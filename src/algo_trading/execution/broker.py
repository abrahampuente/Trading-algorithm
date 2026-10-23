"""
Contrato de broker (Hito 3): cuenta, posiciones y ciclo de vida de órdenes.

`AlpacaPaperBroker` lo implementa contra Alpaca Paper Trading; los tests y el
futuro shadow backtest usan dobles en memoria con el mismo contrato.
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from algo_trading.execution.orders import OrderRequest, OrderState


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    account_number: str
    status: str
    currency: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal
    trading_blocked: bool


class BrokerPosition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    market_value: Decimal | None = None


class BrokerAdapter(Protocol):
    def get_account(self) -> AccountSnapshot: ...

    def get_positions(self) -> Sequence[BrokerPosition]: ...

    def submit_order(self, request: OrderRequest) -> OrderState: ...

    def get_order(self, client_order_id: str) -> OrderState | None: ...

    def list_open_orders(self) -> Sequence[OrderState]: ...

    def cancel_order(self, broker_order_id: str) -> None: ...
