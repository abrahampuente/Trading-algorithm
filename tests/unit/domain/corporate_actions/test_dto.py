from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from algo_trading.domain.corporate_actions import (
    CorporateDividend,
    CorporateSplit,
)


def test_valid_corporate_split() -> None:
    split = CorporateSplit(
        symbol="AAPL",
        ex_date=date(2024, 1, 2),
        ratio=Decimal("2"),
        source="test",
    )

    assert split.symbol == "AAPL"
    assert split.ratio == Decimal("2")


def test_valid_reverse_split() -> None:
    split = CorporateSplit(
        symbol="AAPL",
        ex_date=date(2024, 1, 2),
        ratio=Decimal("0.5"),
        source="test",
    )

    assert split.ratio == Decimal("0.5")


def test_split_rejects_ratio_equal_to_one() -> None:
    with pytest.raises(ValidationError, match="distinto de 1"):
        CorporateSplit(
            symbol="AAPL",
            ex_date=date(2024, 1, 2),
            ratio=Decimal("1"),
            source="test",
        )


def test_split_rejects_non_positive_ratio() -> None:
    with pytest.raises(ValidationError):
        CorporateSplit(
            symbol="AAPL",
            ex_date=date(2024, 1, 2),
            ratio=Decimal("0"),
            source="test",
        )


def test_valid_corporate_dividend() -> None:
    dividend = CorporateDividend(
        symbol="AAPL",
        ex_date=date(2024, 2, 9),
        amount=Decimal("0.24"),
        source="test",
    )

    assert dividend.symbol == "AAPL"
    assert dividend.amount == Decimal("0.24")


def test_dividend_rejects_non_positive_amount() -> None:
    with pytest.raises(ValidationError):
        CorporateDividend(
            symbol="AAPL",
            ex_date=date(2024, 2, 9),
            amount=Decimal("0"),
            source="test",
        )


def test_corporate_split_is_immutable() -> None:
    split = CorporateSplit(
        symbol="AAPL",
        ex_date=date(2024, 1, 2),
        ratio=Decimal("2"),
        source="test",
    )

    with pytest.raises(ValidationError):
        split.ratio = Decimal("3")
