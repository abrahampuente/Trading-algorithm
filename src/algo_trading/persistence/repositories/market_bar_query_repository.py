from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from algo_trading.persistence.models.market_data import MarketBarRaw


class RawMarketBarQueryRepository:
    """Repositorio de lectura para barras raw."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_symbol(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: str = "1d",
        source: str | None = None,
    ) -> Sequence[MarketBarRaw]:
        """Obtiene barras ordenadas cronológicamente para un símbolo."""
        statement = (
            select(MarketBarRaw)
            .where(
                MarketBarRaw.symbol == symbol,
                MarketBarRaw.timestamp >= start,
                MarketBarRaw.timestamp < end,
                MarketBarRaw.timeframe == timeframe,
            )
            .order_by(MarketBarRaw.timestamp)
        )

        if source is not None:
            statement = statement.where(MarketBarRaw.source == source)

        return self._session.scalars(statement).all()

    def get_by_snapshot_id(
        self,
        snapshot_id: str,
    ) -> Sequence[MarketBarRaw]:
        """Obtiene las barras raw pertenecientes a un snapshot."""
        statement = (
            select(MarketBarRaw)
            .where(MarketBarRaw.snapshot_id == snapshot_id)
            .order_by(
                MarketBarRaw.symbol,
                MarketBarRaw.timestamp,
            )
        )

        return self._session.scalars(statement).all()
