from decimal import Decimal

import pytest

from algo_trading.backtesting import BacktestParameters
from algo_trading.backtesting.sensitivity import (
    DEFAULT_MAX_POSITION_EXPOSURES as DEFAULT_EXPOSURES,
)
from algo_trading.backtesting.sensitivity import (
    EXTENDED_TAKE_PROFIT_ATR_MULTIPLES,
    ParameterCombination,
    ParameterGrid,
    SensitivityReport,
    SensitivityRow,
    sensitivity_to_markdown,
)


def test_default_grid_is_three_by_three() -> None:
    grid = ParameterGrid()

    assert grid.stop_loss_atr_multiples == (
        Decimal("1.0"),
        Decimal("1.5"),
        Decimal("2.0"),
    )
    assert grid.max_position_exposures == (
        Decimal("0.10"),
        Decimal("0.15"),
        Decimal("0.20"),
    )
    assert len(grid.combinations()) == 9
    assert grid.take_profit_atr_multiples == (Decimal("2.0"),)
    assert grid.combinations()[1] == ParameterCombination(
        stop_loss_atr_multiple=Decimal("1.0"),
        take_profit_atr_multiple=Decimal("2.0"),
        max_position_exposure=Decimal("0.15"),
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"stop_loss_atr_multiples": (Decimal("0"),)}, "stop loss"),
        ({"max_position_exposures": (Decimal("1.5"),)}, "exposiciones"),
        ({"max_position_exposures": (Decimal("0"),)}, "exposiciones"),
        ({"stop_loss_atr_multiples": (Decimal("1"), Decimal("1"))}, "duplicados"),
        ({"stop_loss_atr_multiples": ()}, "at least 1"),
    ],
)
def test_grid_validation(kwargs: dict[str, tuple[Decimal, ...]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ParameterGrid(**kwargs)  # type: ignore[arg-type]


def row(
    stop_loss: str,
    exposure: str,
    cumulative_return: str,
    drawdown: str,
) -> SensitivityRow:
    return SensitivityRow(
        stop_loss_atr_multiple=Decimal(stop_loss),
        take_profit_atr_multiple=Decimal("2"),
        max_position_exposure=Decimal(exposure),
        run_id=None,
        cumulative_return=Decimal(cumulative_return),
        max_drawdown_pct=Decimal(drawdown),
        win_rate=Decimal("0.45"),
        profit_factor=None,
        expectancy_r=Decimal("0.1"),
        total_trades=10,
        final_equity=Decimal("100000"),
        total_costs=Decimal("100"),
    )


def build_report() -> SensitivityReport:
    return SensitivityReport(
        sweep_id="sweep-1",
        symbols=("AAA", "BBB"),
        start="2024-01-01T00:00:00+00:00",
        end="2024-12-31T00:00:00+00:00",
        trading_calendar="NYSE",
        base_parameters=BacktestParameters(
            initial_cash=Decimal("100000"),
            cost_profile="base",
        ),
        grid=ParameterGrid(
            stop_loss_atr_multiples=(Decimal("1"), Decimal("2")),
            max_position_exposures=(Decimal("0.1"),),
        ),
        buy_and_hold_return=Decimal("0.25"),
        rows=(
            row("1", "0.1", "0.05", "0.04"),
            row("2", "0.1", "0.08", "0.06"),
        ),
    )


def test_best_rows() -> None:
    report = build_report()

    assert report.best_by_return().stop_loss_atr_multiple == Decimal("2")
    assert report.best_by_drawdown().stop_loss_atr_multiple == Decimal("1")


def test_markdown_table_has_one_row_per_combination() -> None:
    markdown = sensitivity_to_markdown(build_report())

    assert markdown.startswith("# Análisis de sensibilidad")
    assert "- Barrido: `sweep-1`" in markdown
    assert "- Perfil de costes: base (comisión 0.05 %, slippage 0.05 %)" in markdown
    assert "- Buy & Hold del periodo: 25.00 %" in markdown
    assert (
        "| 1 | 2 | 10.00 % | 5 | 0.50 % | 5.00 % | 4.00 % | 45.00 % | n/d | 0.10 "
        "| 10 | n/d |" in (markdown)
    )
    assert "| 2 | 2 | 10.00 % | 5 | 0.50 % | 8.00 % | 6.00 % |" in markdown
    assert (
        "- Mayor retorno: SL 2 × ATR, TP 2 × ATR, exposición 10.00 % (8.00 %)."
        in markdown
    )
    assert (
        "- Menor drawdown: SL 1 × ATR, TP 2 × ATR, exposición 10.00 % (4.00 %)."
        in markdown
    )


def test_extended_grid_has_27_combinations_in_grid_order() -> None:
    grid = ParameterGrid(take_profit_atr_multiples=EXTENDED_TAKE_PROFIT_ATR_MULTIPLES)

    combinations = grid.combinations()

    assert len(combinations) == 27
    assert [c.take_profit_atr_multiple for c in combinations[:9:3]] == [
        Decimal("2.0"),
        Decimal("2.5"),
        Decimal("3.0"),
    ]
    assert combinations[-1] == ParameterCombination(
        stop_loss_atr_multiple=Decimal("2.0"),
        take_profit_atr_multiple=Decimal("3.0"),
        max_position_exposure=Decimal("0.20"),
    )


def test_combination_applies_risk_parameters() -> None:
    combination = ParameterCombination(
        stop_loss_atr_multiple=Decimal("1.5"),
        take_profit_atr_multiple=Decimal("2.5"),
        max_position_exposure=Decimal("0.15"),
    )

    parameters = combination.apply(BacktestParameters(initial_cash=Decimal("1000")))

    assert parameters.risk.stop_loss_atr_multiple == Decimal("1.5")
    assert parameters.risk.take_profit_atr_multiple == Decimal("2.5")
    assert parameters.risk.max_position_exposure == Decimal("0.15")
    assert parameters.risk.risk_per_trade == Decimal("0.005")


def test_take_profit_multiples_must_be_positive() -> None:
    with pytest.raises(ValueError, match="take profit"):
        ParameterGrid(take_profit_atr_multiples=(Decimal("0"),))


def test_portfolio_scale_axes_default_to_v1_values() -> None:
    grid = ParameterGrid()

    assert grid.max_positions == (5,)
    assert grid.risk_per_trade == (Decimal("0.005"),)
    assert grid.scale_levels() == ((5, Decimal("0.005")),)


def test_portfolio_scaling_grid_combinations() -> None:
    grid = ParameterGrid(
        stop_loss_atr_multiples=(Decimal("1.5"), Decimal("2.0")),
        take_profit_atr_multiples=(Decimal("2.5"), Decimal("3.0")),
        max_position_exposures=DEFAULT_EXPOSURES,
        max_positions=(5, 8, 10),
        risk_per_trade=(Decimal("0.005"), Decimal("0.010"), Decimal("0.015")),
    )

    combinations = grid.combinations()

    assert len(combinations) == 108
    assert len(grid.scale_levels()) == 9
    assert combinations[0].scale == (5, Decimal("0.005"))
    assert combinations[1].scale == (5, Decimal("0.010"))
    assert combinations[3].scale == (8, Decimal("0.005"))
    assert combinations[-1] == ParameterCombination(
        stop_loss_atr_multiple=Decimal("2.0"),
        take_profit_atr_multiple=Decimal("3.0"),
        max_position_exposure=Decimal("0.20"),
        max_positions=10,
        risk_per_trade=Decimal("0.015"),
    )


def test_combination_applies_portfolio_scale() -> None:
    combination = ParameterCombination(
        stop_loss_atr_multiple=Decimal("1.5"),
        take_profit_atr_multiple=Decimal("3"),
        max_position_exposure=Decimal("0.15"),
        max_positions=8,
        risk_per_trade=Decimal("0.01"),
    )

    risk = combination.apply(BacktestParameters(initial_cash=Decimal("1000"))).risk

    assert risk.max_positions == 8
    assert risk.risk_per_trade == Decimal("0.01")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_positions": (0,)}, "posiciones"),
        ({"risk_per_trade": (Decimal("0"),)}, "riesgo"),
        ({"risk_per_trade": (Decimal("1.5"),)}, "riesgo"),
        ({"max_positions": (5, 5)}, "duplicados"),
    ],
)
def test_portfolio_scale_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ParameterGrid(**kwargs)  # type: ignore[arg-type]
