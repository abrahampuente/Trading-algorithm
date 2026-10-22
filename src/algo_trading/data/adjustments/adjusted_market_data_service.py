from collections.abc import Iterable, Sequence

from sqlalchemy.orm import Session

from algo_trading.data.adjustments.adjusted_bar_builder import (
    AdjustedMarketBarBuilder,
)
from algo_trading.domain.corporate_actions import CorporateSplit
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
        self._adjustment_version = adjustment_version
        self._builder = AdjustedMarketBarBuilder(adjustment_version)
        self._repository = AdjustedMarketBarRepository(session)

    def rebuild_for_symbol(
        self,
        symbol: str,
        source: str,
        timeframe: str,
        raw_bars: Sequence[MarketBarRaw],
        splits: Iterable[CorporateSplit],
    ) -> int:
        """
        Regenera todas las barras ajustadas de un símbolo en una transacción.

        Un split nuevo cambia el ajuste de todo el historial anterior, por lo
        que se reemplazan las barras de esta versión de ajuste en lugar de
        añadir sólo las nuevas.
        """
        if any(
            bar.symbol != symbol or bar.source != source or bar.timeframe != timeframe
            for bar in raw_bars
        ):
            raise ValueError(
                "Las barras raw deben pertenecer al símbolo, source y timeframe "
                "indicados"
            )

        split_events = tuple(splits)
        adjusted_bars = [
            self._builder.build(raw_bar, split_events) for raw_bar in raw_bars
        ]

        try:
            self._repository.delete_for_symbol(
                symbol=symbol,
                source=source,
                timeframe=timeframe,
                adjustment_version=self._adjustment_version,
            )
            entities = self._repository.add_many(adjusted_bars)
            self._repository.flush()
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        return len(entities)

    def build_and_persist(
        self,
        raw_bars: Sequence[MarketBarRaw],
        splits_by_symbol: dict[str, Iterable[CorporateSplit]],
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
