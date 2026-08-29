from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from algo_trading.persistence.models.adjusted_market_data import (
    MarketBarAdjusted,
)


class DuplicateAdjustedMarketBarError(Exception):
    """Indica que una barra ajustada ya existe."""


class AdjustedMarketBarRepository:
    """Repositorio de escritura para barras ajustadas."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, bar: MarketBarAdjusted) -> MarketBarAdjusted:
        """Añade una barra ajustada sin confirmar la transacción."""
        self._session.add(bar)
        return bar

    def add_many(
        self,
        bars: Sequence[MarketBarAdjusted],
    ) -> list[MarketBarAdjusted]:
        """Añade varias barras ajustadas sin confirmar la transacción."""
        self._session.add_all(bars)
        return list(bars)

    def flush(self) -> None:
        """Envía los cambios pendientes sin confirmar la transacción."""
        try:
            self._session.flush()
        except IntegrityError as error:
            self._session.rollback()
            raise DuplicateAdjustedMarketBarError(
                "Una o más barras ajustadas ya existen"
            ) from error
