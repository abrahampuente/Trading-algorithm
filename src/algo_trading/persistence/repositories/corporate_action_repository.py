from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)
from algo_trading.persistence.models.corporate_actions import (
    CorporateAction,
    CorporateActionType,
)

CorporateActionDto = CorporateSplit | CorporateDividend


class DuplicateCorporateActionError(Exception):
    """Indica que una acción corporativa ya existe."""


class CorporateActionRepository:
    """Repositorio de escritura para acciones corporativas."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        action: CorporateActionDto,
        snapshot_id: str | None = None,
    ) -> CorporateAction:
        """Añade una acción corporativa sin confirmar la transacción."""
        entity = self._to_entity(action, snapshot_id=snapshot_id)
        self._session.add(entity)
        return entity

    def add_many(
        self,
        actions: Sequence[CorporateActionDto],
        snapshot_id: str | None = None,
    ) -> list[CorporateAction]:
        """Añade varias acciones corporativas sin confirmar la transacción."""
        entities = [
            self._to_entity(action, snapshot_id=snapshot_id) for action in actions
        ]
        self._session.add_all(entities)
        return entities

    def flush(self) -> None:
        """
        Envía los cambios pendientes a la base de datos.

        No hace commit. La capa de servicio mantiene la responsabilidad
        de la transacción completa.
        """
        try:
            self._session.flush()
        except IntegrityError as error:
            self._session.rollback()
            raise DuplicateCorporateActionError(
                "Una o más acciones corporativas ya existen"
            ) from error

    @staticmethod
    def _to_entity(
        action: CorporateActionDto,
        snapshot_id: str | None = None,
    ) -> CorporateAction:
        if isinstance(action, CorporateSplit):
            return CorporateAction(
                symbol=action.symbol,
                action_type=CorporateActionType.SPLIT,
                ex_date=action.ex_date,
                split_ratio=action.ratio,
                dividend_amount=None,
                source=action.source,
                snapshot_id=snapshot_id,
                announced_at=None,
            )
        return CorporateAction(
            symbol=action.symbol,
            action_type=CorporateActionType.DIVIDEND,
            ex_date=action.ex_date,
            split_ratio=None,
            dividend_amount=action.amount,
            source=action.source,
            snapshot_id=snapshot_id,
            announced_at=None,
        )
