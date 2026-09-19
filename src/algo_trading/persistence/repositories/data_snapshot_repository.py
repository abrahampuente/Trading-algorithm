from datetime import UTC

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from algo_trading.data.providers.dto import DataSnapshot
from algo_trading.persistence.models.data_snapshot import DataSnapshotModel


class DuplicateDataSnapshotError(Exception):
    """Indica que un snapshot ya existe en la base de datos."""


class DataSnapshotRepository:
    """Repositorio de escritura y consulta de metadatos de snapshots."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, snapshot: DataSnapshot) -> DataSnapshotModel:
        """Añade un snapshot a la sesión sin confirmar la transacción."""
        entity = self._to_entity(snapshot)
        self._session.add(entity)
        return entity

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
            raise DuplicateDataSnapshotError(
                "Ya existe un snapshot con el mismo snapshot_id"
            ) from error

    def get_by_snapshot_id(self, snapshot_id: str) -> DataSnapshot | None:
        """Obtiene un snapshot por su identificador o None si no existe."""
        statement = select(DataSnapshotModel).where(
            DataSnapshotModel.snapshot_id == snapshot_id
        )

        entity = self._session.scalar(statement)

        if entity is None:
            return None

        return self._to_dto(entity)

    @staticmethod
    def _to_entity(snapshot: DataSnapshot) -> DataSnapshotModel:
        return DataSnapshotModel(
            snapshot_id=snapshot.snapshot_id,
            source=snapshot.source,
            retrieved_at=snapshot.retrieved_at.replace(tzinfo=None),
            timeframe=snapshot.timeframe,
            bars_count=snapshot.bars_count,
            splits_count=snapshot.splits_count,
            dividends_count=snapshot.dividends_count,
            checksum=snapshot.checksum,
        )

    @staticmethod
    def _to_dto(entity: DataSnapshotModel) -> DataSnapshot:
        return DataSnapshot(
            snapshot_id=entity.snapshot_id,
            source=entity.source,
            retrieved_at=entity.retrieved_at.replace(tzinfo=UTC),
            timeframe=entity.timeframe,
            bars_count=entity.bars_count,
            splits_count=entity.splits_count,
            dividends_count=entity.dividends_count,
            checksum=entity.checksum,
        )
