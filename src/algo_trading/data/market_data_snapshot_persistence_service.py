from sqlalchemy.orm import Session

from algo_trading.data.providers.dto import MarketDataSnapshot
from algo_trading.data.validation.market_bar_validator import validate_market_bars
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)
from algo_trading.persistence.repositories.corporate_action_repository import (
    CorporateActionRepository,
)
from algo_trading.persistence.repositories.data_snapshot_repository import (
    DataSnapshotRepository,
)
from algo_trading.persistence.repositories.market_bar_repository import (
    RawMarketBarRepository,
)


class MarketDataSnapshotPersistenceService:
    """
    Persiste un MarketDataSnapshot en una única transacción.

    Guarda primero los metadatos del snapshot, sus barras raw y sus
    acciones corporativas. Si cualquier operación falla, no queda
    ningún dato parcial persistido.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._snapshot_repository = DataSnapshotRepository(session)
        self._bar_repository = RawMarketBarRepository(session)
        self._corporate_action_repository = CorporateActionRepository(session)

    def persist(self, snapshot: MarketDataSnapshot) -> None:
        """Valida y persiste atómicamente el snapshot completo."""
        validated_bars = validate_market_bars(snapshot.bars)
        corporate_actions: tuple[CorporateSplit | CorporateDividend, ...] = (
            *snapshot.splits,
            *snapshot.dividends,
        )

        try:
            self._snapshot_repository.add(snapshot.metadata)
            self._snapshot_repository.flush()

            self._bar_repository.add_many(
                validated_bars,
                snapshot_id=snapshot.metadata.snapshot_id,
            )
            self._corporate_action_repository.add_many(
                corporate_actions,
                snapshot_id=snapshot.metadata.snapshot_id,
            )

            self._bar_repository.flush()
            self._corporate_action_repository.flush()

            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
