from decimal import Decimal

import pytest

from algo_trading.execution import ExitReason, evaluate_bar_exit, evaluate_time_exit
from algo_trading.portfolio import calculate_long_exit_levels

# Entrada 100, ATR 2 → SL 98, TP 104.
LEVELS = calculate_long_exit_levels(entry_price=Decimal("100"), entry_atr=Decimal("2"))


def test_levels_fixture() -> None:
    assert LEVELS.stop_loss == Decimal("98")
    assert LEVELS.take_profit == Decimal("104")


@pytest.mark.parametrize(
    ("open_price", "high", "low"),
    [
        ("100", "101", "99"),
        ("100", "103.99", "98.01"),
    ],
)
def test_no_exit_when_levels_are_not_reached(
    open_price: str,
    high: str,
    low: str,
) -> None:
    assert (
        evaluate_bar_exit(Decimal(open_price), Decimal(high), Decimal(low), LEVELS)
        is None
    )


@pytest.mark.parametrize(
    ("open_price", "high", "low", "expected_reason", "expected_price"),
    [
        # Sólo TP.
        ("100", "104", "99", ExitReason.TAKE_PROFIT, "104"),
        ("100", "110", "99", ExitReason.TAKE_PROFIT, "104"),
        # Gap alcista sobre el TP: se ejecuta al TP, sin mejora de precio.
        ("105", "106", "104.5", ExitReason.TAKE_PROFIT, "104"),
        # Sólo SL.
        ("100", "101", "98", ExitReason.STOP_LOSS, "98"),
        ("100", "101", "95", ExitReason.STOP_LOSS, "98"),
        # Gap bajista bajo el SL: se ejecuta a la apertura, peor que el SL.
        ("97", "97.5", "96", ExitReason.STOP_LOSS, "97"),
    ],
)
def test_single_level_exits(
    open_price: str,
    high: str,
    low: str,
    expected_reason: ExitReason,
    expected_price: str,
) -> None:
    decision = evaluate_bar_exit(
        Decimal(open_price),
        Decimal(high),
        Decimal(low),
        LEVELS,
    )

    assert decision is not None
    assert decision.reason is expected_reason
    assert decision.theoretical_price == Decimal(expected_price)


@pytest.mark.parametrize(
    ("open_price", "high", "low", "expected_price"),
    [
        # Toca ambos exactamente.
        ("100", "104", "98", "98"),
        # Rango amplio que cruza ambos.
        ("100", "110", "90", "98"),
        # Abre cerca del TP: el orden real probablemente fue TP primero,
        # pero sin granularidad se asume SL.
        ("103.9", "104.5", "97", "98"),
        # Abre sobre el TP y luego cae bajo el SL: prioridad absoluta del SL.
        ("105", "106", "97", "98"),
        # Gap bajista bajo el SL con máximo sobre el TP: SL a la apertura.
        ("96", "105", "95", "96"),
    ],
)
def test_stop_loss_has_priority_when_bar_reaches_both_levels(
    open_price: str,
    high: str,
    low: str,
    expected_price: str,
) -> None:
    decision = evaluate_bar_exit(
        Decimal(open_price),
        Decimal(high),
        Decimal(low),
        LEVELS,
    )

    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.theoretical_price == Decimal(expected_price)


@pytest.mark.parametrize(
    ("open_price", "expected_reason"),
    [
        ("99", ExitReason.TIME_EXIT),
        ("105", ExitReason.TIME_EXIT),
        ("98", ExitReason.STOP_LOSS),
        ("97", ExitReason.STOP_LOSS),
    ],
)
def test_time_exit_executes_at_open(
    open_price: str,
    expected_reason: ExitReason,
) -> None:
    decision = evaluate_time_exit(Decimal(open_price), LEVELS)

    assert decision.reason is expected_reason
    assert decision.theoretical_price == Decimal(open_price)
