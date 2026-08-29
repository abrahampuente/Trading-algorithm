from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MarketBar(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    timestamp: datetime
    timeframe: str = Field(min_length=1, max_length=16)
    source: str = Field(min_length=1, max_length=64)

    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_ohlc_relationships(self) -> "MarketBar":
        if self.high < max(self.open, self.close):
            raise ValueError("high debe ser mayor o igual que open y close")

        if self.low > min(self.open, self.close):
            raise ValueError("low debe ser menor o igual que open y close")

        if self.low > self.high:
            raise ValueError("low no puede ser mayor que high")

        return self
