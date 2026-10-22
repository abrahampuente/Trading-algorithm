from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
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

    def get_all_by_symbol(
        self,
        symbol: str,
        timeframe: str,
        source: str,
    ) -> Sequence[MarketBarRaw]:
        """Obtiene todo el historial raw de un símbolo, en orden cronológico."""
        statement = (
            select(MarketBarRaw)
            .where(
                MarketBarRaw.symbol == symbol,
                MarketBarRaw.timeframe == timeframe,
                MarketBarRaw.source == source,
            )
            .order_by(MarketBarRaw.timestamp)
        )

        return self._session.scalars(statement).all()

    def get_latest_timestamp(
        self,
        symbol: str,
        timeframe: str,
        source: str,
    ) -> datetime | None:
        """Devuelve el timestamp de la última barra raw almacenada, o None."""
        statement = select(func.max(MarketBarRaw.timestamp)).where(
            MarketBarRaw.symbol == symbol,
            MarketBarRaw.timeframe == timeframe,
            MarketBarRaw.source == source,
        )

        return self._session.scalar(statement)

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
