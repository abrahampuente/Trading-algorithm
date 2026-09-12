from collections.abc import Sequence
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from algo_trading.domain.corporate_actions import (
    CorporateActionMapper,
    CorporateSplit,
)
from algo_trading.persistence.models.corporate_actions import (
    CorporateAction,
    CorporateActionType,
)


class CorporateActionQueryRepository:
    """Repositorio de lectura para acciones corporativas."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_splits_by_symbol(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        source: str | None = None,
    ) -> Sequence[CorporateAction]:
        """Obtiene splits ordenados por fecha ex-date."""

        statement = (
            select(CorporateAction)
            .where(
                CorporateAction.symbol == symbol,
                CorporateAction.action_type == CorporateActionType.SPLIT,
            )
            .order_by(CorporateAction.ex_date)
        )

        if start is not None:
            statement = statement.where(CorporateAction.ex_date >= start)

        if end is not None:
            statement = statement.where(CorporateAction.ex_date < end)

        if source is not None:
            statement = statement.where(CorporateAction.source == source)

        return self._session.scalars(statement).all()

    def get_split_dtos_by_symbol(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
        source: str | None = None,
    ) -> Sequence[CorporateSplit]:
        """Obtiene splits convertidos a DTOs de dominio."""

        actions = self.get_splits_by_symbol(
            symbol=symbol,
            start=start,
            end=end,
            source=source,
        )

        splits: list[CorporateSplit] = []

        for action in actions:
            domain_action = CorporateActionMapper.to_domain(action)

            if not isinstance(domain_action, CorporateSplit):
                raise TypeError(
                    "El repositorio de splits produjo un DTO que no es CorporateSplit"
                )

            splits.append(domain_action)

        return splits
