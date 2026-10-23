from decimal import Decimal

import pytest

from algo_trading.backtesting import BacktestParameters
from algo_trading.backtesting.sensitivity import (
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
    assert grid.combinations()[1] == (Decimal("1.0"), Decimal("0.15"))


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
    assert "| 1 | 10.00 % | 5.00 % | 4.00 % | 45.00 % | n/d | 0.10 | 10 | n/d |" in (
        markdown
    )
    assert "| 2 | 10.00 % | 8.00 % | 6.00 % |" in markdown
    assert "- Mayor retorno: SL 2 × ATR, exposición 10.00 % (8.00 %)." in markdown
    assert "- Menor drawdown: SL 1 × ATR, exposición 10.00 % (4.00 %)." in markdown
