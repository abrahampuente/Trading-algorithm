from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)
from algo_trading.persistence.models.corporate_actions import (
    CorporateAction,
    CorporateActionType,
)


class CorporateActionMapper:
    """Convierte modelos de persistencia en DTOs de dominio."""

    @staticmethod
    def to_domain(
        action: CorporateAction,
    ) -> CorporateSplit | CorporateDividend:
        if action.action_type == CorporateActionType.SPLIT:
            if action.split_ratio is None:
                raise ValueError("Una acción SPLIT debe tener split_ratio")

            return CorporateSplit(
                symbol=action.symbol,
                ex_date=action.ex_date,
                ratio=action.split_ratio,
                source=action.source,
            )

        if action.action_type == CorporateActionType.DIVIDEND:
            if action.dividend_amount is None:
                raise ValueError("Una acción DIVIDEND debe tener dividend_amount")

            return CorporateDividend(
                symbol=action.symbol,
                ex_date=action.ex_date,
                amount=action.dividend_amount,
                source=action.source,
            )

        raise ValueError(
            f"Tipo de acción corporativa no soportado: {action.action_type}"
        )
