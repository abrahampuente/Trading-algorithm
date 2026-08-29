from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from algo_trading.data.market_bar import MarketBar
from algo_trading.persistence.models.market_data import MarketBarRaw


class DuplicateMarketBarError(Exception):
    """Indica que una barra ya existe en la base de datos."""


class RawMarketBarRepository:
    """Repositorio de escritura para barras raw de mercado."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, bar: MarketBar) -> MarketBarRaw:
        """Añade una barra a la sesión sin confirmar la transacción."""
        entity = self._to_entity(bar)
        self._session.add(entity)
        return entity

    def add_many(self, bars: Sequence[MarketBar]) -> list[MarketBarRaw]:
        """Añade varias barras a la sesión sin confirmar la transacción."""
        entities = [self._to_entity(bar) for bar in bars]
        self._session.add_all(entities)
        return entities

    def flush(self) -> None:
        """
        Envía los cambios pendientes a la base de datos.

        No hace commit. La transacción sigue siendo responsabilidad
        de la capa de servicio u orquestación.
        """
        try:
            self._session.flush()
        except IntegrityError as error:
            self._session.rollback()
            raise DuplicateMarketBarError(
                "Una o más barras ya existen en market_bars_raw"
            ) from error

    @staticmethod
    def _to_entity(bar: MarketBar) -> MarketBarRaw:
        return MarketBarRaw(
            symbol=bar.symbol,
            timestamp=bar.timestamp.replace(tzinfo=None),
            timeframe=bar.timeframe,
            source=bar.source,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
