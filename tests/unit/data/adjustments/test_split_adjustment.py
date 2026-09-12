from datetime import date
from decimal import Decimal

import pytest

from algo_trading.data.adjustments import SplitAdjustmentCalculator
from algo_trading.domain.corporate_actions import CorporateSplit


def test_split_adjusts_bars_before_ex_date() -> None:
    factor = SplitAdjustmentCalculator.factor_for_bar(
        bar_date=date(2024, 1, 1),
        ex_date=date(2024, 1, 2),
        split_ratio=Decimal("2"),
    )

    assert factor == Decimal("0.5")


def test_split_does_not_adjust_ex_date_or_later() -> None:
    calculator = SplitAdjustmentCalculator

    assert calculator.factor_for_bar(
        bar_date=date(2024, 1, 2),
        ex_date=date(2024, 1, 2),
        split_ratio=Decimal("2"),
    ) == Decimal("1")

    assert calculator.factor_for_bar(
        bar_date=date(2024, 1, 3),
        ex_date=date(2024, 1, 2),
        split_ratio=Decimal("2"),
    ) == Decimal("1")


def test_adjust_price() -> None:
    adjusted_price = SplitAdjustmentCalculator.adjust_price(
        price=Decimal("100"),
        factor=Decimal("0.5"),
    )

    assert adjusted_price == Decimal("50")


def test_adjust_volume() -> None:
    adjusted_volume = SplitAdjustmentCalculator.adjust_volume(
        volume=Decimal("1000000"),
        factor=Decimal("0.5"),
    )

    assert adjusted_volume == Decimal("2000000")


def test_rejects_invalid_split_ratio() -> None:
    with pytest.raises(ValueError, match="split_ratio"):
        SplitAdjustmentCalculator.factor_for_bar(
            bar_date=date(2024, 1, 1),
            ex_date=date(2024, 1, 2),
            split_ratio=Decimal("0"),
        )


def test_cumulative_factor_for_multiple_splits() -> None:
    factor = SplitAdjustmentCalculator.cumulative_factor_for_bar(
        bar_date=date(2020, 1, 1),
        splits=[
            CorporateSplit(
                symbol="AAPL",
                ex_date=date(2021, 1, 2),
                ratio=Decimal("2"),
                source="test",
            ),
            CorporateSplit(
                symbol="AAPL",
                ex_date=date(2022, 1, 2),
                ratio=Decimal("3"),
                source="test",
            ),
        ],
    )

    assert factor == Decimal("1") / Decimal("6")


def test_cumulative_factor_ignores_splits_before_or_on_bar_date() -> None:
    factor = SplitAdjustmentCalculator.cumulative_factor_for_bar(
        bar_date=date(2022, 1, 2),
        splits=[
            CorporateSplit(
                symbol="AAPL",
                ex_date=date(2021, 1, 2),
                ratio=Decimal("2"),
                source="test",
            ),
            CorporateSplit(
                symbol="AAPL",
                ex_date=date(2022, 1, 2),
                ratio=Decimal("3"),
                source="test",
            ),
            CorporateSplit(
                symbol="AAPL",
                ex_date=date(2023, 1, 2),
                ratio=Decimal("5"),
                source="test",
            ),
        ],
    )

    assert factor == Decimal("0.2")


def test_cumulative_factor_rejects_invalid_ratio() -> None:
    with pytest.raises(ValueError, match="ratio"):
        SplitAdjustmentCalculator.cumulative_factor_for_bar(
            bar_date=date(2020, 1, 1),
            splits=[
                CorporateSplit(
                    symbol="AAPL",
                    ex_date=date(2021, 1, 2),
                    ratio=Decimal("0"),
                    source="test",
                ),
            ],
        )
