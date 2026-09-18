from collections.abc import Iterable, Sequence

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.providers.dto import (
    CorporateActionRequest,
    DataRequest,
    DataSnapshot,
    MarketDataSnapshot,
)
from algo_trading.data.providers.protocol import MarketDataProvider
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)


class InMemoryMarketDataProvider:
    def __init__(
        self,
        bars: Iterable[MarketBar] = (),
        splits: Iterable[CorporateSplit] = (),
        dividends: Iterable[CorporateDividend] = (),
    ) -> None:
        self._bars = tuple(bars)
        self._splits = tuple(splits)
        self._dividends = tuple(dividends)

    def get_bars(self, request: DataRequest) -> Sequence[MarketBar]:
        return tuple(
            bar
            for bar in self._bars
            if bar.symbol in request.symbols
            and request.start <= bar.timestamp <= request.end
            and bar.timeframe == request.timeframe
            and bar.source == request.source
        )

    def get_splits(self, request: CorporateActionRequest) -> Sequence[CorporateSplit]:
        return tuple(
            split
            for split in self._splits
            if split.symbol in request.symbols
            and request.start <= split.ex_date <= request.end
            and split.source == request.source
        )

    def get_dividends(
        self, request: CorporateActionRequest
    ) -> Sequence[CorporateDividend]:
        return tuple(
            dividend
            for dividend in self._dividends
            if dividend.symbol in request.symbols
            and request.start <= dividend.ex_date <= request.end
            and dividend.source == request.source
        )

    def get_snapshot(
        self,
        data_request: DataRequest,
        corporate_action_request: CorporateActionRequest,
    ) -> MarketDataSnapshot:
        bars = tuple(self.get_bars(data_request))
        splits = tuple(self.get_splits(corporate_action_request))
        dividends = tuple(self.get_dividends(corporate_action_request))
        metadata = DataSnapshot(
            snapshot_id=(
                f"{data_request.source}:{data_request.timeframe}:"
                f"{data_request.start.isoformat()}:{data_request.end.isoformat()}"
            ),
            source=data_request.source,
            retrieved_at=data_request.end,
            timeframe=data_request.timeframe,
            bars_count=len(bars),
            splits_count=len(splits),
            dividends_count=len(dividends),
            checksum="in-memory",
        )
        return MarketDataSnapshot(
            metadata=metadata,
            bars=bars,
            splits=splits,
            dividends=dividends,
        )


_PROVIDER_PROTOCOL_CHECK: type[MarketDataProvider] = InMemoryMarketDataProvider
