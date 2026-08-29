from collections.abc import Sequence

from sqlalchemy.orm import Session

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.validation.market_bar_validator import validate_market_bars
from algo_trading.persistence.repositories import RawMarketBarRepository


class IngestionService:
    """Coordina la validación y persistencia de barras de mercado."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = RawMarketBarRepository(session)

    def ingest_bars(self, bars: Sequence[MarketBar]) -> int:
        """
        Valida y persiste una colección de barras.

        La transacción se confirma completamente si todo funciona.
        Si ocurre cualquier error, se revierte completamente.
        """
        validated_bars = validate_market_bars(bars)

        try:
            entities = self._repository.add_many(validated_bars)
            self._repository.flush()
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        return len(entities)
