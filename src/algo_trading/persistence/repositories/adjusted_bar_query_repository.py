from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from algo_trading.persistence.models.adjusted_market_data import (
    MarketBarAdjusted,
)


class AdjustedMarketBarQueryRepository:
    """Repositorio de lectura para barras ajustadas."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_symbol(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1d",
        source: str | None = None,
        adjustment_version: str | None = None,
    ) -> Sequence[MarketBarAdjusted]:
        """
        Obtiene barras ajustadas en el intervalo semiabierto [start, end).

        Las barras se ordenan cronológicamente.
        """
        statement = (
            select(MarketBarAdjusted)
            .where(
                MarketBarAdjusted.symbol == symbol,
                MarketBarAdjusted.timestamp >= start,
                MarketBarAdjusted.timestamp < end,
                MarketBarAdjusted.timeframe == timeframe,
            )
            .order_by(MarketBarAdjusted.timestamp)
        )

        if source is not None:
            statement = statement.where(
                MarketBarAdjusted.source == source,
            )

        if adjustment_version is not None:
            statement = statement.where(
                MarketBarAdjusted.adjustment_version == adjustment_version,
            )

        return self._session.scalars(statement).all()
