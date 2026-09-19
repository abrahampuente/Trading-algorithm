from sqlalchemy.orm import Session

from algo_trading.data.providers.dto import DataSnapshot, MarketDataSnapshot
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


class SnapshotContentConflictError(ValueError):
    """
    Indica que un snapshot_id existente representa contenido diferente.

    Un snapshot_id debe identificar siempre el mismo contenido canónico.
    """


class MarketDataSnapshotPersistenceService:
    """
    Persiste un MarketDataSnapshot en una única transacción.

    Un reintento con el mismo snapshot_id y checksum es idempotente:
    no genera inserciones duplicadas y devuelve el snapshot existente.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._snapshot_repository = DataSnapshotRepository(session)
        self._bar_repository = RawMarketBarRepository(session)
        self._corporate_action_repository = CorporateActionRepository(session)

    def persist(self, snapshot: MarketDataSnapshot) -> DataSnapshot:
        """Valida y persiste atómicamente el snapshot completo."""
        existing_snapshot = self._snapshot_repository.get_by_snapshot_id(
            snapshot.metadata.snapshot_id
        )

        if existing_snapshot is not None:
            self._validate_existing_snapshot(
                existing_snapshot=existing_snapshot,
                incoming_snapshot=snapshot.metadata,
            )
            return existing_snapshot

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

        return snapshot.metadata

    @staticmethod
    def _validate_existing_snapshot(
        existing_snapshot: DataSnapshot,
        incoming_snapshot: DataSnapshot,
    ) -> None:
        if existing_snapshot.checksum != incoming_snapshot.checksum:
            raise SnapshotContentConflictError(
                "El snapshot_id ya existe con un checksum diferente"
            )

        if existing_snapshot.source != incoming_snapshot.source:
            raise SnapshotContentConflictError(
                "El snapshot_id ya existe con un source diferente"
            )

        if existing_snapshot.timeframe != incoming_snapshot.timeframe:
            raise SnapshotContentConflictError(
                "El snapshot_id ya existe con un timeframe diferente"
            )
