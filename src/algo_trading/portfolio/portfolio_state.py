from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Position(BaseModel):
    """Posición Long abierta, valorada al último precio conocido."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    quantity: int = Field(ge=1)
    entry_price: Decimal = Field(gt=0)
    stop_loss: Decimal
    take_profit: Decimal
    market_price: Decimal = Field(gt=0)

    @property
    def market_value(self) -> Decimal:
        return self.market_price * self.quantity


class PortfolioState(BaseModel):
    """
    Estado inmutable del portfolio en un instante.

    `equity = cash + valor de mercado de las posiciones abiertas`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cash: Decimal = Field(ge=0)
    positions: tuple[Position, ...] = ()

    @model_validator(mode="after")
    def validate_positions(self) -> Self:
        symbols = [position.symbol for position in self.positions]

        if len(set(symbols)) != len(symbols):
            raise ValueError("Sólo se permite una posición por símbolo")

        return self

    @property
    def market_value(self) -> Decimal:
        return sum(
            (position.market_value for position in self.positions),
            Decimal("0"),
        )

    @property
    def equity(self) -> Decimal:
        return self.cash + self.market_value

    @property
    def open_symbols(self) -> frozenset[str]:
        return frozenset(position.symbol for position in self.positions)

    def has_position(self, symbol: str) -> bool:
        return symbol in self.open_symbols
