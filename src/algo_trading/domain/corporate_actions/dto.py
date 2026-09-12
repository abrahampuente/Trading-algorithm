from datetime import date
from decimal import Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CorporateSplit(BaseModel):
    """Split corporativo expresado como ratio posterior/anterior."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    ex_date: date
    ratio: Decimal = Field(gt=0)
    source: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_ratio(self) -> Self:
        if self.ratio == Decimal("1"):
            raise ValueError("Un split debe tener un ratio distinto de 1")

        return self


class CorporateDividend(BaseModel):
    """Dividendo efectivo por acción en la fecha ex-dividendo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=32)
    ex_date: date
    amount: Decimal = Field(gt=0)
    source: str = Field(min_length=1, max_length=64)
