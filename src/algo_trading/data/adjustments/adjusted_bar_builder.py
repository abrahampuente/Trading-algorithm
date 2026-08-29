from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from algo_trading.data.adjustments.split_adjustment import (
    SplitAdjustmentCalculator,
)
from algo_trading.persistence.models.adjusted_market_data import (
    MarketBarAdjusted,
)
from algo_trading.persistence.models.market_data import MarketBarRaw


class AdjustedMarketBarBuilder:
    """Construye barras ajustadas a partir de barras raw y splits."""

    def __init__(self, adjustment_version: str) -> None:
        if not adjustment_version.strip():
            raise ValueError("adjustment_version no puede estar vacío")

        self._adjustment_version = adjustment_version

    def build(
        self,
        raw_bar: MarketBarRaw,
        splits: Iterable[tuple[date, Decimal]],
    ) -> MarketBarAdjusted:
        split_events = sorted(splits, key=lambda event: event[0])

        factor = SplitAdjustmentCalculator.cumulative_factor_for_bar(
            bar_date=raw_bar.timestamp.date(),
            splits=split_events,
        )

        return MarketBarAdjusted(
            symbol=raw_bar.symbol,
            timestamp=raw_bar.timestamp,
            timeframe=raw_bar.timeframe,
            source=raw_bar.source,
            open=SplitAdjustmentCalculator.adjust_price(raw_bar.open, factor),
            high=SplitAdjustmentCalculator.adjust_price(raw_bar.high, factor),
            low=SplitAdjustmentCalculator.adjust_price(raw_bar.low, factor),
            close=SplitAdjustmentCalculator.adjust_price(raw_bar.close, factor),
            volume=SplitAdjustmentCalculator.adjust_volume(
                raw_bar.volume,
                factor,
            ),
            adjustment_factor=factor,
            adjustment_version=self._adjustment_version,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
