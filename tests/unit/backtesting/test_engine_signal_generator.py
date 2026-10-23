import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.analytics.report import build_report, report_to_json
from algo_trading.backtesting import BacktestEngine, BacktestParameters
from algo_trading.data.market_bar import MarketBar
from algo_trading.indicators.price_series import PriceBar
from algo_trading.persistence.repositories.backtest_run_repository import (
    _run_parameters,
)
from algo_trading.signals import (
    CompositeSignalGenerator,
    FilterResult,
    MomentumBreakoutGenerator,
    Side,
    Signal,
    SignalContext,
    VixRegimeFilter,
    long_signal_from_signal,
)
from algo_trading.strategy import LongSignalGenerator

START = datetime(2024, 1, 1, tzinfo=UTC)
PARAMETERS = BacktestParameters(initial_cash=Decimal("100000"))


def random_bars(symbol: str, seed: int, length: int = 400) -> list[MarketBar]:
    generator = random.Random(seed)
    close = Decimal("100")
    bars = []

    for day in range(length):
        open_price = max(
            Decimal("5"), close + Decimal(generator.randint(-60, 80)) / 100
        )
        close = max(
            Decimal("5"), open_price + Decimal(generator.randint(-150, 180)) / 100
        )
        bars.append(
            MarketBar(
                symbol=symbol,
                timestamp=START + timedelta(days=day),
                timeframe="1d",
                source="test",
                open=open_price,
                high=max(open_price, close) + Decimal("0.5"),
                low=max(Decimal("1"), min(open_price, close) - Decimal("0.5")),
                close=close,
                volume=Decimal("1000"),
            )
        )

    return bars


def universe() -> dict[str, list[MarketBar]]:
    return {"AAA": random_bars("AAA", 1), "BBB": random_bars("BBB", 2)}


def constant_vix(level: str, length: int = 400) -> SignalContext:
    return SignalContext(
        {
            "^VIX": [
                MarketBar(
                    symbol="^VIX",
                    timestamp=START + timedelta(days=day),
                    timeframe="1d",
                    source="test",
                    open=Decimal(level),
                    high=Decimal(level),
                    low=Decimal(level),
                    close=Decimal(level),
                    volume=Decimal("0"),
                )
                for day in range(length)
            ]
        }
    )


class RejectAll:
    @property
    def name(self) -> str:
        return "reject_all"

    def evaluate(
        self,
        signal: Signal,
        bars: Sequence[PriceBar],
        context: SignalContext,
    ) -> FilterResult:
        return FilterResult(name=self.name, passed=False, reason="test")


def test_generator_without_filters_reproduces_v1_engine() -> None:
    bars = universe()
    v1 = BacktestEngine(PARAMETERS).run(bars)

    for generator in (
        MomentumBreakoutGenerator(),
        CompositeSignalGenerator(MomentumBreakoutGenerator()),
    ):
        result = BacktestEngine(PARAMETERS).run(bars, signal_generator=generator)

        assert v1.trades
        assert result.trades == v1.trades
        assert result.gap_filtered == v1.gap_filtered
        assert result.rejected == v1.rejected
        assert result.equity_curve == v1.equity_curve
        assert result.final_cash == v1.final_cash
        assert result.signal_strategy_id == generator.strategy_id
        assert result.filtered_signals == ()


def test_filtered_signals_are_recorded_and_never_traded() -> None:
    bars = universe()
    base_signals = sum(
        len(LongSignalGenerator().generate(series)) for series in bars.values()
    )

    result = BacktestEngine(PARAMETERS).run(
        bars,
        signal_generator=CompositeSignalGenerator(
            MomentumBreakoutGenerator(),
            filters=[RejectAll()],
            strategy_id="blocked",
        ),
    )

    assert result.trades == ()
    assert result.gap_filtered == ()
    assert len(result.filtered_signals) == base_signals
    assert result.signal_strategy_id == "blocked"


@pytest.mark.parametrize(("level", "blocks"), [("18", False), ("35", True)])
def test_vix_filter_uses_signal_context(level: str, blocks: bool) -> None:
    bars = universe()
    v1 = BacktestEngine(PARAMETERS).run(bars)

    result = BacktestEngine(PARAMETERS).run(
        bars,
        signal_generator=CompositeSignalGenerator(
            MomentumBreakoutGenerator(),
            filters=[VixRegimeFilter(max_level=Decimal("25"))],
        ),
        signal_context=constant_vix(level),
    )

    if blocks:
        assert result.trades == ()
        assert result.filtered_signals
    else:
        assert result.trades == v1.trades
        assert result.filtered_signals == ()


def test_precomputed_signals_and_generator_are_exclusive() -> None:
    with pytest.raises(ValueError, match="excluyentes"):
        BacktestEngine(PARAMETERS).run(
            universe(),
            precomputed_signals={},
            signal_generator=MomentumBreakoutGenerator(),
        )


def test_report_json_only_includes_signal_filters_with_generator() -> None:
    bars = universe()
    generator = CompositeSignalGenerator(
        MomentumBreakoutGenerator(),
        filters=[VixRegimeFilter(max_level=Decimal("25"))],
        strategy_id="momentum_breakout_v1+vix",
    )

    v1_report = build_report(BacktestEngine(PARAMETERS).run(bars), bars)
    filtered_report = build_report(
        BacktestEngine(PARAMETERS).run(
            bars, signal_generator=generator, signal_context=constant_vix("35")
        ),
        bars,
        signal_generator=generator,
    )

    assert v1_report.signal_filters is None
    assert "signal_filters" not in report_to_json(v1_report)
    assert "signal_filters" not in _run_parameters(v1_report)

    summary = filtered_report.signal_filters
    assert summary is not None
    assert summary.strategy_id == "momentum_breakout_v1+vix"
    assert summary.filters == (
        {"name": "vix_regime", "symbol": "^VIX", "max_level": "25"},
    )
    assert summary.filtered_signals > 0
    assert summary.filtered_by == {"vix_regime": summary.filtered_signals}
    assert summary.filtered_reasons == {}
    assert _run_parameters(filtered_report)["signal_filters"]["strategy_id"] == (
        "momentum_breakout_v1+vix"
    )
    assert (
        type(filtered_report).model_validate_json(report_to_json(filtered_report))
        == filtered_report
    )


def test_long_signal_round_trip_and_validation() -> None:
    bars = random_bars("AAA", 1)
    v1_signals = LongSignalGenerator().generate(bars)
    signals = MomentumBreakoutGenerator().generate(bars)

    assert tuple(long_signal_from_signal(signal) for signal in signals) == v1_signals

    with pytest.raises(ValueError, match="LONG"):
        long_signal_from_signal(signals[0].model_copy(update={"side": Side.SHORT}))

    with pytest.raises(ValueError, match="upper_band"):
        long_signal_from_signal(signals[0].model_copy(update={"factors": {}}))
