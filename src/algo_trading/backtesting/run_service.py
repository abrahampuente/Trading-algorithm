from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from algo_trading.analytics.report import BacktestReport, build_report
from algo_trading.backtesting.engine import BacktestEngine, BacktestParameters
from algo_trading.data.market_bar import MarketBar
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted
from algo_trading.persistence.repositories.adjusted_bar_query_repository import (
    AdjustedMarketBarQueryRepository,
)


class MissingMarketDataError(ValueError):
    """Indica que algún símbolo no tiene barras ajustadas en el periodo."""


class BacktestRunRequest(BaseModel):
    """
    Qué datos ajustados leer de MySQL y con qué parámetros simular.

    `start` y `end` son fechas de sesión inclusivas.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = Field(min_length=1)
    start: date
    end: date
    parameters: BacktestParameters
    source: str = Field(default="yfinance", min_length=1, max_length=64)
    timeframe: str = Field(default="1d", min_length=1, max_length=16)
    adjustment_version: str = Field(default="splits-v1", min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.end < self.start:
            raise ValueError("end no puede ser anterior a start")

        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols no puede contener duplicados")

        return self


class BacktestRunService:
    """
    Ejecuta un backtest sobre barras ajustadas persistidas.

    Lee `market_bars_adjusted` (sólo lectura), convierte las filas a
    `MarketBar` con timestamp UTC, ejecuta `BacktestEngine` y construye el
    `BacktestReport`. No escribe en la base de datos.
    """

    def __init__(self, session: Session) -> None:
        self._repository = AdjustedMarketBarQueryRepository(session)

    def load_bars(
        self, request: BacktestRunRequest
    ) -> dict[str, tuple[MarketBar, ...]]:
        start = datetime(request.start.year, request.start.month, request.start.day)
        # El repositorio usa [start, end); se suma un día para incluir `end`.
        end = datetime(
            request.end.year, request.end.month, request.end.day
        ) + timedelta(days=1)

        bars_by_symbol: dict[str, tuple[MarketBar, ...]] = {}
        missing: list[str] = []

        for symbol in request.symbols:
            rows = self._repository.get_by_symbol(
                symbol=symbol,
                start=start,
                end=end,
                timeframe=request.timeframe,
                source=request.source,
                adjustment_version=request.adjustment_version,
            )

            if not rows:
                missing.append(symbol)
                continue

            bars_by_symbol[symbol] = tuple(_to_market_bar(row) for row in rows)

        if missing:
            raise MissingMarketDataError(
                "Sin barras ajustadas en el periodo para: " + ", ".join(missing)
            )

        return bars_by_symbol

    def run(self, request: BacktestRunRequest) -> BacktestReport:
        bars_by_symbol = self.load_bars(request)
        result = BacktestEngine(request.parameters).run(bars_by_symbol)
        return build_report(result, bars_by_symbol)


def _to_market_bar(row: MarketBarAdjusted) -> MarketBar:
    return MarketBar(
        symbol=row.symbol,
        timestamp=row.timestamp.replace(tzinfo=UTC),
        timeframe=row.timeframe,
        source=row.source,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
    )


def default_report_name(symbols: Sequence[str], start: date, end: date) -> str:
    """Nombre determinista: `backtest_<símbolos>_<inicio>_<fin>`."""
    return f"backtest_{'-'.join(sorted(symbols))}_{start.isoformat()}_{end.isoformat()}"
