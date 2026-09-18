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
    def validate_snapshot(self) -> Self:
        if self.metadata.bars_count != len(self.bars):
            raise ValueError("bars_count no coincide con el número de barras")

        if self.metadata.splits_count != len(self.splits):
            raise ValueError("splits_count no coincide con el número de splits")

        if self.metadata.dividends_count != len(self.dividends):
            raise ValueError("dividends_count no coincide con el número de dividendos")

        self._validate_bar_sources()
        self._validate_split_sources()
        self._validate_dividend_sources()
        self._validate_duplicate_bars()
        self._validate_duplicate_splits()
        self._validate_duplicate_dividends()

        return self

    def _validate_bar_sources(self) -> None:
        for bar in self.bars:
            if bar.source != self.metadata.source:
                raise ValueError(
                    "Todas las barras deben utilizar el source del snapshot"
                )

            if bar.timeframe != self.metadata.timeframe:
                raise ValueError(
                    "Todas las barras deben utilizar el timeframe del snapshot"
                )

    def _validate_split_sources(self) -> None:
        for split in self.splits:
            if split.source != self.metadata.source:
                raise ValueError(
                    "Todos los splits deben utilizar el source del snapshot"
                )

    def _validate_dividend_sources(self) -> None:
        for dividend in self.dividends:
            if dividend.source != self.metadata.source:
                raise ValueError(
                    "Todos los dividendos deben utilizar el source del snapshot"
                )

    def _validate_duplicate_bars(self) -> None:
        identities = {
            (bar.symbol, bar.timestamp, bar.timeframe, bar.source) for bar in self.bars
        }

        if len(identities) != len(self.bars):
            raise ValueError("El snapshot contiene barras duplicadas")

    def _validate_duplicate_splits(self) -> None:
        identities = {
            (split.symbol, split.ex_date, split.source) for split in self.splits
        }

        if len(identities) != len(self.splits):
            raise ValueError("El snapshot contiene splits duplicados")

    def _validate_duplicate_dividends(self) -> None:
        identities = {
            (dividend.symbol, dividend.ex_date, dividend.source)
            for dividend in self.dividends
        }

        if len(identities) != len(self.dividends):
            raise ValueError("El snapshot contiene dividendos duplicados")
