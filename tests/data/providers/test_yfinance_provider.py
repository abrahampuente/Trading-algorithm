from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from algo_trading.data.adjustments.split_adjustment import SplitAdjustmentCalculator
from algo_trading.data.providers.dto import CorporateActionRequest, DataRequest
from algo_trading.data.providers.snapshot_checksum import (
    calculate_snapshot_checksum,
)
from algo_trading.data.providers.yfinance import (
    YahooCorporateEvent,
    YahooDailyRow,
    YahooHistory,
    YFinanceProvider,
)

RETRIEVED_AT = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


def row(
    session_date: date,
    close: str,
    volume: str = "4000",
    high: str | None = None,
) -> YahooDailyRow:
    value = Decimal(close)
    return YahooDailyRow(
        session_date=session_date,
        open=value,
        high=Decimal(high) if high is not None else value + 1,
        low=value - 1,
        close=value,
        volume=Decimal(volume),
    )


def aapl_like_history() -> YahooHistory:
    """Split 4:1 el 2020-08-31; precios y dividendos ya ajustados como Yahoo."""
    return YahooHistory(
        rows=(
            row(date(2020, 8, 27), "125", volume="160000000"),
            row(date(2020, 8, 28), "124.8"),
            row(date(2020, 8, 31), "129.04"),
            row(date(2020, 9, 1), "134.18"),
        ),
        splits=(YahooCorporateEvent(ex_date=date(2020, 8, 31), value=Decimal("4")),),
        dividends=(
            YahooCorporateEvent(ex_date=date(2020, 8, 7), value=Decimal("0.205")),
            YahooCorporateEvent(ex_date=date(2020, 11, 6), value=Decimal("0.205")),
        ),
    )


class FakeFetcher:
    def __init__(self, history: YahooHistory) -> None:
        self.history = history
        self.calls: list[tuple[str, date, date]] = []

    def __call__(self, symbol: str, start: date, end: date) -> YahooHistory:
        self.calls.append((symbol, start, end))
        return self.history


def build_provider(history: YahooHistory) -> tuple[YFinanceProvider, FakeFetcher]:
    fetcher = FakeFetcher(history)
    return YFinanceProvider(fetcher=fetcher, clock=lambda: RETRIEVED_AT), fetcher


def data_request(start: date, end: date, symbol: str = "AAPL") -> DataRequest:
    return DataRequest(
        symbols=(symbol,),
        start=datetime(start.year, start.month, start.day, tzinfo=UTC),
        end=datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC),
        timeframe="1d",
        source="yfinance",
    )


def action_request(start: date, end: date) -> CorporateActionRequest:
    return CorporateActionRequest(
        symbols=("AAPL",),
        start=start,
        end=end,
        source="yfinance",
    )


def test_reconstructs_raw_prices_and_volume_before_split() -> None:
    provider, _ = build_provider(aapl_like_history())

    bars = provider.get_bars(data_request(date(2020, 8, 27), date(2020, 9, 1)))

    by_date = {bar.timestamp.date(): bar for bar in bars}
    # Antes del split: precio x4 y volumen /4.
    assert by_date[date(2020, 8, 27)].close == Decimal("500")
    assert by_date[date(2020, 8, 27)].high == Decimal("504")
    assert by_date[date(2020, 8, 27)].volume == Decimal("40000000")
    assert by_date[date(2020, 8, 28)].close == Decimal("499.2")
    # Desde el ex_date: sin cambios.
    assert by_date[date(2020, 8, 31)].close == Decimal("129.04")
    assert by_date[date(2020, 9, 1)].close == Decimal("134.18")


def test_split_after_requested_window_still_unadjusts_prices() -> None:
    provider, _ = build_provider(aapl_like_history())

    bars = provider.get_bars(data_request(date(2020, 8, 27), date(2020, 8, 28)))
    splits = provider.get_splits(action_request(date(2020, 8, 27), date(2020, 8, 28)))

    assert [bar.close for bar in bars] == [Decimal("500"), Decimal("499.2")]
    assert splits == ()


def test_split_adjustment_of_raw_bars_recovers_yahoo_prices() -> None:
    history = aapl_like_history()
    provider, _ = build_provider(history)
    window = (date(2020, 8, 27), date(2020, 9, 1))

    bars = provider.get_bars(data_request(*window))
    splits = provider.get_splits(action_request(*window))

    for bar, yahoo_row in zip(bars, history.rows, strict=True):
        factor = SplitAdjustmentCalculator.cumulative_factor_for_bar(
            bar.timestamp.date(), splits
        )
        assert bar.close * factor == yahoo_row.close
        assert bar.volume / factor == yahoo_row.volume


def test_cumulative_splits_are_all_undone() -> None:
    history = YahooHistory(
        rows=(row(date(2014, 1, 2), "27.85"),),
        splits=(
            YahooCorporateEvent(ex_date=date(2014, 6, 9), value=Decimal("7")),
            YahooCorporateEvent(ex_date=date(2020, 8, 31), value=Decimal("4")),
        ),
        dividends=(),
    )
    provider, _ = build_provider(history)

    bars = provider.get_bars(data_request(date(2014, 1, 2), date(2014, 1, 2)))

    assert bars[0].close == Decimal("779.8")  # 27.85 * 7 * 4


def test_dividends_are_unadjusted_and_filtered_by_window() -> None:
    provider, _ = build_provider(aapl_like_history())

    dividends = provider.get_dividends(
        action_request(date(2020, 8, 1), date(2020, 12, 31))
    )

    assert [(d.ex_date, d.amount) for d in dividends] == [
        (date(2020, 8, 7), Decimal("0.82")),  # 0.205 * 4 (antes del split)
        (date(2020, 11, 6), Decimal("0.205")),
    ]


def test_daily_timestamp_is_session_date_at_midnight_utc() -> None:
    provider, _ = build_provider(aapl_like_history())

    bars = provider.get_bars(data_request(date(2020, 8, 31), date(2020, 8, 31)))

    assert [bar.timestamp for bar in bars] == [datetime(2020, 8, 31, tzinfo=UTC)]
    assert bars[0].source == "yfinance"
    assert bars[0].timeframe == "1d"


def test_invalid_rows_are_discarded_and_reported() -> None:
    history = YahooHistory(
        rows=(
            row(date(2024, 1, 2), "100"),
            row(date(2024, 1, 3), "101", high="100.5"),  # high < close
        ),
        splits=(),
        dividends=(),
    )
    provider, _ = build_provider(history)

    bars = provider.get_bars(data_request(date(2024, 1, 2), date(2024, 1, 3)))

    assert [bar.timestamp.date() for bar in bars] == [date(2024, 1, 2)]
    assert len(provider.rejected_rows) == 1
    assert provider.rejected_rows[0].session_date == date(2024, 1, 3)


def test_snapshot_has_stable_id_valid_checksum_and_single_download() -> None:
    provider, fetcher = build_provider(aapl_like_history())
    window = (date(2020, 8, 1), date(2020, 9, 1))

    snapshot = provider.get_snapshot(data_request(*window), action_request(*window))

    metadata = snapshot.metadata
    assert metadata.snapshot_id == "yfinance:1d:AAPL:2020-08-01:2020-09-01"
    assert metadata.retrieved_at == RETRIEVED_AT
    assert metadata.bars_count == 4
    assert metadata.splits_count == 1
    assert metadata.dividends_count == 1
    assert metadata.checksum == calculate_snapshot_checksum(
        bars=snapshot.bars,
        splits=snapshot.splits,
        dividends=snapshot.dividends,
    )
    assert fetcher.calls == [("AAPL", date(2020, 8, 1), date(2020, 9, 1))]


def test_rejects_unsupported_source_and_timeframe() -> None:
    provider, _ = build_provider(aapl_like_history())

    with pytest.raises(ValueError, match="source"):
        provider.get_bars(
            DataRequest(
                symbols=("AAPL",),
                start=datetime(2020, 1, 1, tzinfo=UTC),
                end=datetime(2020, 2, 1, tzinfo=UTC),
                timeframe="1d",
                source="other",
            )
        )

    with pytest.raises(ValueError, match="1d"):
        provider.get_bars(
            DataRequest(
                symbols=("AAPL",),
                start=datetime(2020, 1, 1, tzinfo=UTC),
                end=datetime(2020, 2, 1, tzinfo=UTC),
                timeframe="intraday",
                source="yfinance",
            )
        )
