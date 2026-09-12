from datetime import date
from decimal import Decimal

import pytest

from algo_trading.domain.corporate_actions import (
    CorporateActionMapper,
    CorporateDividend,
    CorporateSplit,
)
from algo_trading.persistence.models.corporate_actions import (
    CorporateAction,
    CorporateActionType,
)


def test_maps_split_to_domain_dto() -> None:
    action = CorporateAction(
        symbol="AAPL",
        action_type=CorporateActionType.SPLIT,
        ex_date=date(2024, 1, 2),
        split_ratio=Decimal("2"),
        source="test",
    )

    result = CorporateActionMapper.to_domain(action)

    assert isinstance(result, CorporateSplit)
    assert result.symbol == "AAPL"
    assert result.ratio == Decimal("2")


def test_maps_dividend_to_domain_dto() -> None:
    action = CorporateAction(
        symbol="AAPL",
        action_type=CorporateActionType.DIVIDEND,
        ex_date=date(2024, 2, 9),
        dividend_amount=Decimal("0.24"),
        source="test",
    )

    result = CorporateActionMapper.to_domain(action)

    assert isinstance(result, CorporateDividend)
    assert result.symbol == "AAPL"
    assert result.amount == Decimal("0.24")


def test_rejects_split_without_ratio() -> None:
    action = CorporateAction(
        symbol="AAPL",
        action_type=CorporateActionType.SPLIT,
        ex_date=date(2024, 1, 2),
        source="test",
    )

    with pytest.raises(ValueError, match="split_ratio"):
        CorporateActionMapper.to_domain(action)


def test_rejects_dividend_without_amount() -> None:
    action = CorporateAction(
        symbol="AAPL",
        action_type=CorporateActionType.DIVIDEND,
        ex_date=date(2024, 2, 9),
        source="test",
    )

    with pytest.raises(ValueError, match="dividend_amount"):
        CorporateActionMapper.to_domain(action)
