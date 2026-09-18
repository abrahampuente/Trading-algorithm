from datetime import date, datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from algo_trading.data.market_bar import MarketBar
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)


class Timeframe(StrEnum):
    DAILY = "1d"
    INTRADAY = "intraday"


class DataRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = Field(min_length=1)
    start: datetime
    end: datetime
    timeframe: str = Field(min_length=1, max_length=16)
    source: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end debe ser posterior a start")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols no puede contener duplicados")
        return self


class CorporateActionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = Field(min_length=1)
    start: date
    end: date
    source: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.end < self.start:
            raise ValueError("end no puede ser anterior a start")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols no puede contener duplicados")
        return self


class DataSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_id: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    retrieved_at: datetime
    timeframe: str = Field(min_length=1, max_length=16)
    bars_count: int = Field(ge=0)
    splits_count: int = Field(ge=0)
    dividends_count: int = Field(ge=0)
    checksum: str = Field(min_length=1, max_length=128)


class MarketDataSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: DataSnapshot
    bars: tuple[MarketBar, ...] = ()
    splits: tuple[CorporateSplit, ...] = ()
    dividends: tuple[CorporateDividend, ...] = ()

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.metadata.bars_count != len(self.bars):
            raise ValueError("bars_count no coincide con el número de barras")
        if self.metadata.splits_count != len(self.splits):
            raise ValueError("splits_count no coincide con el número de splits")
        if self.metadata.dividends_count != len(self.dividends):
            raise ValueError("dividends_count no coincide con el número de dividendos")
        return self
