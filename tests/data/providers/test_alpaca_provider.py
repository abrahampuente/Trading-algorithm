import json
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from algo_trading.data.market_calendar import TradingSession
from algo_trading.data.providers.alpaca import (
    AlpacaAdjustment,
    AlpacaCredentials,
    AlpacaFeed,
    AlpacaMarketDataProvider,
    AlpacaRequestError,
    AlpacaSessionBarSource,
    AlpacaUnavailableError,
    HttpResponse,
    RetryPolicy,
    build_snapshot_id,
)
from algo_trading.data.providers.dto import CorporateActionRequest, DataRequest
from algo_trading.data.providers.snapshot_checksum import calculate_snapshot_checksum

NOW = datetime(2024, 3, 5, 12, 0, tzinfo=UTC)
CREDENTIALS = AlpacaCredentials(key_id="test-key", secret_key="test-secret")
REQUEST = DataRequest(
    symbols=("MSFT", "AAPL"),
    start=datetime(2024, 3, 4, 14, 30, tzinfo=UTC),
    end=datetime(2024, 3, 4, 21, 0, tzinfo=UTC),
    timeframe="1m",
    source="alpaca-iex",
)


def bar_json(minute: int, close: str = "100.12") -> dict[str, Any]:
    return {
        "t": f"2024-03-04T14:{30 + minute:02d}:00Z",
        "o": 100.1,
        "h": 100.55,
        "l": 99.95,
        "c": float(close),
        "v": 1234,
        "n": 10,
        "vw": 100.2,
    }


def ok(
    payload: Mapping[str, Any], headers: Mapping[str, str] | None = None
) -> HttpResponse:
    return HttpResponse(
        status=200,
        headers=dict(headers or {}),
        body=json.dumps(payload).encode("utf-8"),
    )


def status(code: int, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status=code, headers=dict(headers or {}), body=b'{"message":"x"}'
    )


class FakeHttp:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def __call__(
        self, url: str, headers: Mapping[str, str], timeout: float
    ) -> HttpResponse:
        self.calls.append((url, dict(headers), timeout))
        response = self.responses.pop(0)

        if isinstance(response, Exception):
            raise response

        return response

    def params(self, index: int) -> dict[str, str]:
        query = urllib.parse.urlparse(self.calls[index][0]).query
        return dict(urllib.parse.parse_qsl(query))


def provider(
    http: FakeHttp, sleeps: list[float], **kwargs: Any
) -> AlpacaMarketDataProvider:
    return AlpacaMarketDataProvider(
        CREDENTIALS,
        http_get=http,
        sleep=sleeps.append,
        clock=lambda: NOW,
        retry=RetryPolicy(max_attempts=4, base_delay_seconds=1, max_delay_seconds=30),
        **kwargs,
    )


def test_parses_bars_as_exact_decimals_and_paginates() -> None:
    http = FakeHttp(
        ok(
            {
                "bars": {"AAPL": [bar_json(0)], "MSFT": [bar_json(1)]},
                "next_page_token": "page-2",
            }
        ),
        ok({"bars": {"AAPL": [bar_json(1, "100.3")]}, "next_page_token": None}),
    )

    bars = provider(http, []).get_bars(REQUEST)

    assert [(bar.symbol, bar.timestamp.minute) for bar in bars] == [
        ("AAPL", 30),
        ("AAPL", 31),
        ("MSFT", 31),
    ]
    first = bars[0]
    assert first.timestamp == datetime(2024, 3, 4, 14, 30, tzinfo=UTC)
    assert first.open == Decimal("100.1")
    assert first.high == Decimal("100.55")
    assert first.close == Decimal("100.12")
    assert first.volume == Decimal("1234")
    assert (first.timeframe, first.source) == ("1m", "alpaca-iex")

    params = http.params(0)
    assert params == {
        "symbols": "AAPL,MSFT",
        "timeframe": "1Min",
        "start": "2024-03-04T14:30:00Z",
        "end": "2024-03-04T21:00:00Z",
        "feed": "iex",
        "adjustment": "raw",
        "limit": "10000",
        "sort": "asc",
    }
    assert http.params(1)["page_token"] == "page-2"
    url, headers, timeout = http.calls[0]
    assert url.startswith("https://data.alpaca.markets/v2/stocks/bars?")
    assert headers["APCA-API-KEY-ID"] == "test-key"
    assert headers["APCA-API-SECRET-KEY"] == "test-secret"
    assert timeout == 30.0


def test_feed_adjustment_and_five_minute_timeframe() -> None:
    http = FakeHttp(ok({"bars": {}, "next_page_token": None}))
    sip = provider(http, [], feed=AlpacaFeed.SIP, adjustment=AlpacaAdjustment.SPLIT)

    sip.get_bars(REQUEST.model_copy(update={"timeframe": "5m", "source": "alpaca-sip"}))

    assert sip.source == "alpaca-sip"
    assert http.params(0)["timeframe"] == "5Min"
    assert http.params(0)["feed"] == "sip"
    assert http.params(0)["adjustment"] == "split"


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"source": "yfinance"}, "alpaca-iex"),
        ({"timeframe": "1d"}, "timeframes"),
    ],
)
def test_rejects_other_sources_and_timeframes(
    update: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        provider(FakeHttp(), []).get_bars(REQUEST.model_copy(update=update))


def test_rate_limit_waits_retry_after_and_retries() -> None:
    sleeps: list[float] = []
    http = FakeHttp(
        status(429, {"retry-after": "3"}),
        ok({"bars": {"AAPL": [bar_json(0)]}}),
    )

    bars = provider(http, sleeps).get_bars(REQUEST)

    assert len(bars) == 1
    assert sleeps == [3.0]


def test_rate_limit_without_retry_after_waits_until_quota_reset() -> None:
    sleeps: list[float] = []
    reset = str(int(NOW.timestamp()) + 7)
    http = FakeHttp(
        status(429, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset}),
        ok({"bars": {}}),
    )

    provider(http, sleeps).get_bars(REQUEST)

    assert sleeps == [7.0]


def test_exhausted_quota_delays_the_next_request() -> None:
    sleeps: list[float] = []
    reset = str(int(NOW.timestamp()) + 5)
    http = FakeHttp(
        ok(
            {"bars": {"AAPL": [bar_json(0)]}, "next_page_token": "p2"},
            {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
        ),
        ok({"bars": {}, "next_page_token": None}, {"x-ratelimit-remaining": "199"}),
    )

    provider(http, sleeps).get_bars(REQUEST)

    assert sleeps == [5.0]


def test_server_and_network_errors_back_off_exponentially() -> None:
    sleeps: list[float] = []
    http = FakeHttp(
        status(500),
        OSError("conexión reiniciada"),
        status(503),
        ok({"bars": {"AAPL": [bar_json(0)]}}),
    )

    bars = provider(http, sleeps).get_bars(REQUEST)

    assert len(bars) == 1
    assert sleeps == [1.0, 2.0, 4.0]


def test_gives_up_after_max_attempts() -> None:
    sleeps: list[float] = []
    http = FakeHttp(status(502), status(502), status(502), status(502))

    with pytest.raises(AlpacaUnavailableError, match="4 intentos"):
        provider(http, sleeps).get_bars(REQUEST)

    assert len(http.calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(base_delay_seconds=1, max_delay_seconds=10)

    assert [policy.delay(attempt) for attempt in range(6)] == [1, 2, 4, 8, 10, 10]


@pytest.mark.parametrize("code", [401, 403, 422])
def test_client_errors_are_not_retried(code: int) -> None:
    sleeps: list[float] = []
    http = FakeHttp(status(code))

    with pytest.raises(AlpacaRequestError, match=str(code)):
        provider(http, sleeps).get_bars(REQUEST)

    assert len(http.calls) == 1
    assert sleeps == []


def test_inconsistent_bars_are_rejected_and_reported() -> None:
    broken = {**bar_json(1), "h": 90.0}
    http = FakeHttp(ok({"bars": {"AAPL": [bar_json(0), broken]}}))
    alpaca = provider(http, [])

    bars = alpaca.get_bars(REQUEST)

    assert len(bars) == 1
    assert len(alpaca.rejected_bars) == 1
    assert alpaca.rejected_bars[0].symbol == "AAPL"


def test_snapshot_has_bars_only_and_reproducible_checksum() -> None:
    http = FakeHttp(ok({"bars": {"AAPL": [bar_json(0), bar_json(1)]}}))
    actions = CorporateActionRequest(
        symbols=("AAPL",),
        start=date(2024, 3, 4),
        end=date(2024, 3, 4),
        source="alpaca-iex",
    )

    snapshot = provider(http, []).get_snapshot(REQUEST, actions)

    assert snapshot.metadata.snapshot_id == (
        "alpaca-iex:1m:AAPL,MSFT:2024-03-04T14:30:00Z:2024-03-04T21:00:00Z"
    )
    assert snapshot.metadata.snapshot_id == build_snapshot_id(REQUEST)
    assert snapshot.metadata.retrieved_at == NOW
    assert snapshot.metadata.bars_count == 2
    assert snapshot.splits == () and snapshot.dividends == ()
    assert snapshot.metadata.checksum == calculate_snapshot_checksum(
        bars=snapshot.bars, splits=(), dividends=()
    )


class EarlyCloseCalendar:
    name = "TEST"

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        if start != date(2024, 3, 4):
            return ()

        return (
            TradingSession(
                session_date=start,
                market_open=datetime(2024, 3, 4, 14, 30, tzinfo=UTC),
                market_close=datetime(2024, 3, 4, 14, 32, tzinfo=UTC),
                is_early_close=True,
            ),
        )


def test_session_source_requests_regular_session_one_minute_bars() -> None:
    http = FakeHttp(ok({"bars": {"AAPL": [bar_json(0), bar_json(1), bar_json(2)]}}))
    source = AlpacaSessionBarSource(provider(http, []), EarlyCloseCalendar())

    bars = source.get_session_bars("AAPL", date(2024, 3, 4))

    assert [bar.timestamp.minute for bar in bars] == [30, 31]
    params = http.params(0)
    assert params["symbols"] == "AAPL"
    assert params["timeframe"] == "1Min"
    assert (params["start"], params["end"]) == (
        "2024-03-04T14:30:00Z",
        "2024-03-04T14:32:00Z",
    )
    assert source.get_session_bars("AAPL", date(2024, 3, 9)) == ()
