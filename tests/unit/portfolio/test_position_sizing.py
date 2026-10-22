from decimal import Decimal

import pytest

from algo_trading.portfolio import (
    PositionSizer,
    RiskParameters,
    SizingLimit,
    calculate_long_exit_levels,
)

EQUITY = Decimal("100000")


def test_v1_default_risk_parameters() -> None:
    parameters = RiskParameters()

    assert parameters.risk_per_trade == Decimal("0.005")
    assert parameters.max_position_exposure == Decimal("0.10")
    assert parameters.max_positions == 5


def test_quantity_limited_by_risk() -> None:
    # Riesgo 500 / (50 - 45) = 100; exposición 10000 / 50 = 200.
    result = PositionSizer().size(
        equity=EQUITY,
        cash=EQUITY,
        entry_price=Decimal("50"),
        stop_loss=Decimal("45"),
    )

    assert result.risk_amount == Decimal("500")
    assert result.quantity_by_risk == 100
    assert result.quantity_by_exposure == 200
    assert result.quantity_by_capital == 2000
    assert result.quantity == 100
    assert result.limiting_factor is SizingLimit.RISK
    assert result.planned_risk == Decimal("500")
    assert result.notional == Decimal("5000")
    assert result.is_tradable


def test_quantity_limited_by_nominal_exposure() -> None:
    # Riesgo 500 / 2 = 250, pero 10 % del equity sólo permite 200.
    result = PositionSizer().size(
        equity=EQUITY,
        cash=EQUITY,
        entry_price=Decimal("50"),
        stop_loss=Decimal("48"),
    )

    assert result.quantity_by_risk == 250
    assert result.quantity == 200
    assert result.limiting_factor is SizingLimit.EXPOSURE
    assert result.notional == EQUITY * Decimal("0.10")
    assert result.planned_risk == Decimal("400")


def test_quantity_limited_by_available_cash() -> None:
    result = PositionSizer().size(
        equity=EQUITY,
        cash=Decimal("1000"),
        entry_price=Decimal("50"),
        stop_loss=Decimal("45"),
    )

    assert result.quantity_by_capital == 20
    assert result.quantity == 20
    assert result.limiting_factor is SizingLimit.CAPITAL
    assert result.total_cost <= Decimal("1000")


def test_rounds_risk_quantity_down() -> None:
    # 500 / 3 = 166.67 → 166.
    result = PositionSizer().size(
        equity=EQUITY,
        cash=EQUITY,
        entry_price=Decimal("50"),
        stop_loss=Decimal("47"),
    )

    assert result.quantity_by_risk == 166
    assert result.quantity == 166
    assert result.planned_risk == Decimal("498")
    assert result.planned_risk <= result.risk_amount


def test_keeps_exact_integer_quantity_without_rounding_loss() -> None:
    # 500 / 2.5 = 200 exacto.
    result = PositionSizer().size(
        equity=EQUITY,
        cash=EQUITY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("97.5"),
    )

    assert result.quantity_by_risk == 200


def test_rounds_down_just_below_next_unit() -> None:
    # 500 / 2.50001 = 199.9992 → 199.
    result = PositionSizer().size(
        equity=EQUITY,
        cash=EQUITY,
        entry_price=Decimal("100"),
        stop_loss=Decimal("97.49999"),
    )

    assert result.quantity_by_risk == 199


def test_commission_reduces_quantity_affordable_with_cash() -> None:
    # 1000 / (50 * 1.001) = 19.98 → 19; sin comisión serían 20.
    sizer = PositionSizer(RiskParameters(commission_rate=Decimal("0.001")))

    result = sizer.size(
        equity=EQUITY,
        cash=Decimal("1000"),
        entry_price=Decimal("50"),
        stop_loss=Decimal("45"),
    )

    assert result.quantity == 19
    assert result.commission == Decimal("0.950")
    assert result.total_cost <= Decimal("1000")


def test_rejects_trade_when_cash_buys_less_than_one_unit() -> None:
    result = PositionSizer().size(
        equity=EQUITY,
        cash=Decimal("49.99"),
        entry_price=Decimal("50"),
        stop_loss=Decimal("45"),
    )

    assert result.quantity == 0
    assert result.limiting_factor is SizingLimit.CAPITAL
    assert not result.is_tradable


def test_rejects_trade_when_exposure_limit_is_below_one_unit() -> None:
    # 10 % de 10000 = 1000 < precio 1500.
    result = PositionSizer().size(
        equity=Decimal("10000"),
        cash=Decimal("10000"),
        entry_price=Decimal("1500"),
        stop_loss=Decimal("1490"),
    )

    assert result.quantity_by_exposure == 0
    assert not result.is_tradable


def test_rejects_trade_when_risk_per_unit_exceeds_risk_budget() -> None:
    # Riesgo 50 < riesgo por unidad 60.
    result = PositionSizer().size(
        equity=Decimal("10000"),
        cash=Decimal("10000"),
        entry_price=Decimal("100"),
        stop_loss=Decimal("40"),
    )

    assert result.quantity_by_risk == 0
    assert result.limiting_factor is SizingLimit.RISK
    assert not result.is_tradable


def test_zero_equity_produces_untradable_result() -> None:
    result = PositionSizer().size(
        equity=Decimal("0"),
        cash=Decimal("0"),
        entry_price=Decimal("50"),
        stop_loss=Decimal("45"),
    )

    assert result.quantity == 0
    assert not result.is_tradable


def test_tie_reports_risk_as_limiting_factor() -> None:
    # Riesgo 500 / 2.5 = 200 y exposición 10000 / 50 = 200.
    result = PositionSizer().size(
        equity=EQUITY,
        cash=EQUITY,
        entry_price=Decimal("50"),
        stop_loss=Decimal("47.5"),
    )

    assert result.quantity_by_risk == result.quantity_by_exposure == 200
    assert result.limiting_factor is SizingLimit.RISK


@pytest.mark.parametrize(
    ("equity", "cash", "entry_price", "stop_loss", "message"),
    [
        (Decimal("-1"), Decimal("0"), Decimal("50"), Decimal("45"), "equity"),
        (EQUITY, Decimal("-1"), Decimal("50"), Decimal("45"), "cash"),
        (EQUITY, EQUITY, Decimal("0"), Decimal("-1"), "entry_price"),
        (EQUITY, EQUITY, Decimal("50"), Decimal("50"), "stop_loss"),
        (EQUITY, EQUITY, Decimal("50"), Decimal("51"), "stop_loss"),
    ],
)
def test_rejects_invalid_inputs(
    equity: Decimal,
    cash: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PositionSizer().size(
            equity=equity,
            cash=cash,
            entry_price=entry_price,
            stop_loss=stop_loss,
        )


def test_long_exit_levels_use_frozen_entry_atr() -> None:
    levels = calculate_long_exit_levels(
        entry_price=Decimal("50"),
        entry_atr=Decimal("2"),
    )

    assert levels.stop_loss == Decimal("48")
    assert levels.take_profit == Decimal("54")


def test_long_exit_levels_reject_non_positive_atr() -> None:
    with pytest.raises(ValueError, match="entry_atr"):
        calculate_long_exit_levels(entry_price=Decimal("50"), entry_atr=Decimal("0"))
