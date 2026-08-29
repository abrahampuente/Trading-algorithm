from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from algo_trading.data.adjustments.adjusted_bar_builder import (
    AdjustedMarketBarBuilder,
)
from algo_trading.persistence.models.adjusted_market_data import (
    MarketBarAdjusted,
)
from algo_trading.persistence.models.market_data import MarketBarRaw
from algo_trading.persistence.repositories.adjusted_bar_repository import (
    AdjustedMarketBarRepository,
)


class AdjustedMarketDataService:
    """Genera y persiste barras ajustadas a partir de barras raw."""

    def __init__(
        self,
        session: Session,
        adjustment_version: str,
    ) -> None:
        self._session = session
        self._builder = AdjustedMarketBarBuilder(adjustment_version)
        self._repository = AdjustedMarketBarRepository(session)

    def build_and_persist(
        self,
        raw_bars: Sequence[MarketBarRaw],
        splits_by_symbol: dict[str, Iterable[tuple[date, Decimal]]],
    ) -> int:
        """
        Construye y persiste barras ajustadas en una única transacción.

        Si falla una barra, se revierte toda la operación.
        """
        adjusted_bars: list[MarketBarAdjusted] = []

        for raw_bar in raw_bars:
            splits = splits_by_symbol.get(raw_bar.symbol, ())
            adjusted_bar = self._builder.build(raw_bar, splits)
            adjusted_bars.append(adjusted_bar)

        try:
            entities = self._repository.add_many(adjusted_bars)
            self._repository.flush()
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        return len(entities)
