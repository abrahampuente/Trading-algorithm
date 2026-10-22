from decimal import Decimal

import pytest

from algo_trading.execution import is_gap_filtered

SIGNAL_CLOSE = Decimal("100")
ATR = Decimal("2")


@pytest.mark.parametrize(
    ("entry_open", "threshold_atr", "expected"),
    [
        # Umbral 0.5 * ATR 2 = 1.
        (Decimal("100"), Decimal("0.5"), False),
        (Decimal("100.99"), Decimal("0.5"), False),
        (Decimal("101"), Decimal("0.5"), False),  # igual al umbral: se permite
        (Decimal("101.0001"), Decimal("0.5"), True),
        (Decimal("108"), Decimal("0.5"), True),
        # Gaps bajistas nunca se filtran.
        (Decimal("99"), Decimal("0.5"), False),
        (Decimal("90"), Decimal("0.5"), False),
        # Umbral 0: cualquier gap alcista cancela la entrada.
        (Decimal("100"), Decimal("0"), False),
        (Decimal("100.01"), Decimal("0"), True),
        # Umbral 1 * ATR 2 = 2.
        (Decimal("101.5"), Decimal("1"), False),
        (Decimal("102"), Decimal("1"), False),
        (Decimal("102.01"), Decimal("1"), True),
    ],
)
def test_filters_only_upward_gaps_above_threshold(
    entry_open: Decimal,
    threshold_atr: Decimal,
    expected: bool,
) -> None:
    assert (
        is_gap_filtered(
            entry_open=entry_open,
            signal_close=SIGNAL_CLOSE,
            signal_atr=ATR,
            threshold_atr=threshold_atr,
        )
        is expected
    )


def test_threshold_scales_with_signal_atr() -> None:
    entry_open = Decimal("101.5")

    assert is_gap_filtered(entry_open, SIGNAL_CLOSE, Decimal("2"), Decimal("0.5"))
    assert not is_gap_filtered(entry_open, SIGNAL_CLOSE, Decimal("4"), Decimal("0.5"))


def test_rejects_negative_threshold() -> None:
    with pytest.raises(ValueError, match="threshold_atr"):
        is_gap_filtered(Decimal("101"), SIGNAL_CLOSE, ATR, Decimal("-0.1"))


def test_rejects_non_positive_atr() -> None:
    with pytest.raises(ValueError, match="signal_atr"):
        is_gap_filtered(Decimal("101"), SIGNAL_CLOSE, Decimal("0"), Decimal("0.5"))
