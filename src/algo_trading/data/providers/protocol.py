from collections.abc import Sequence
from typing import Protocol

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.providers.dto import (
    CorporateActionRequest,
    DataRequest,
    MarketDataSnapshot,
)
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)


class MarketDataProvider(Protocol):
    def get_bars(self, request: DataRequest) -> Sequence[MarketBar]: ...

    def get_splits(
        self, request: CorporateActionRequest
    ) -> Sequence[CorporateSplit]: ...

    def get_dividends(
        self, request: CorporateActionRequest
    ) -> Sequence[CorporateDividend]: ...

    def get_snapshot(
        self,
        data_request: DataRequest,
        corporate_action_request: CorporateActionRequest,
    ) -> MarketDataSnapshot: ...
