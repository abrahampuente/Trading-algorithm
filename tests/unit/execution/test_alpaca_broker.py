import json
import urllib.parse
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from algo_trading.config.settings import ApplicationSettings
from algo_trading.data.providers.alpaca import HttpResponse, RetryPolicy
from algo_trading.execution.alpaca_broker import (
    AlpacaPaperBroker,
    AlpacaTradingCredentials,
    BrokerUnavailableError,
    LiveTradingNotAllowedError,
    OrderRejectedError,
    build_alpaca_paper_broker,
    order_payload,
    parse_order,
)
from algo_trading.execution.orders import (
    OrderBook,
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)

PAPER_URL = "https://paper-api.alpaca.markets"
CREDENTIALS = AlpacaTradingCredentials(
    key_id="PKTESTKEY0000000000000000", secret_key=SecretStr("secret")
)
ENTRY = OrderRequest(
    client_order_id="algo-entry-AAPL-20240304-abc",
    symbol="AAPL",
    side=OrderSide.BUY,
    quantity=10,
    order_type=OrderType.LIMIT,
    time_in_force=TimeInForce.OPG,
    limit_price=Decimal("176.40"),
)
EXIT = OrderRequest(
    client_order_id="algo-exit-AAPL-20240304-abc",
    symbol="AAPL",
    side=OrderSide.SELL,
    quantity=10,
    order_type=OrderType.LIMIT,
    time_in_force=TimeInForce.GTC,
    order_class=OrderClass.OCO,
    take_profit_limit_price=Decimal("186.85"),
    stop_loss_stop_price=Decimal("169.32"),
)


def account(number: str = "PA3XXXXXXXXX") -> dict[str, Any]:
    return {
        "account_number": number,
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "100000",
        "equity": "100000.5",
        "buying_power": "200000",
        "trading_blocked": False,
    }


def order_json(
    client_order_id: str = ENTRY.client_order_id,
    status: str = "accepted",
    filled: str = "0",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": f"broker-{client_order_id}",
        "client_order_id": client_order_id,
        "symbol": "AAPL",
        "side": "buy",
        "type": "limit",
        "time_in_force": "opg",
        "order_class": "",
        "qty": "10",
        "filled_qty": filled,
        "filled_avg_price": None if filled == "0" else "176.12",
        "limit_price": "176.4",
        "stop_price": None,
        "status": status,
        "submitted_at": "2024-03-04T13:00:01.123456789Z",
        "filled_at": None,
        "updated_at": "2024-03-04T14:30:00.5Z",
        "legs": None,
        **extra,
    }


def response(status: int, payload: Any = None) -> HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return HttpResponse(status=status, headers={}, body=body)


class FakeTransport:
    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        self.calls.append((method, url, dict(headers), body))

        if not self.responses:
            raise AssertionError(f"Petición inesperada: {method} {url}")

        item = self.responses.pop(0)

        if isinstance(item, Exception):
            raise item

        return item

    @property
    def routes(self) -> list[str]:
        return [
            f"{method} {urllib.parse.urlparse(url).path}"
            for method, url, _, _ in self.calls
        ]


def broker(
    transport: FakeTransport,
    paper_flag: str | None = "true",
    base_url: str = PAPER_URL,
    credentials: AlpacaTradingCredentials = CREDENTIALS,
    sleeps: list[float] | None = None,
) -> AlpacaPaperBroker:
    return AlpacaPaperBroker(
        credentials,
        paper_flag=paper_flag,
        base_url=base_url,
        retry=RetryPolicy(max_attempts=3, base_delay_seconds=1),
        http_send=transport,
        sleep=(sleeps if sleeps is not None else []).append,
    )


# --- Salvaguarda de paper trading ------------------------------------------


@pytest.mark.parametrize("flag", [None, "", "false", "1", "yes", "True ish", "live"])
def test_paper_flag_must_be_exactly_true(flag: str | None) -> None:
    transport = FakeTransport()

    with pytest.raises(LiveTradingNotAllowedError, match="ALPACA_PAPER"):
        broker(transport, paper_flag=flag)

    assert transport.calls == []


@pytest.mark.parametrize("flag", ["true", "TRUE", " True "])
def test_paper_flag_accepts_true_in_any_case(flag: str) -> None:
    broker(FakeTransport(), paper_flag=flag)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://evil.example",
    ],
)
def test_only_the_paper_url_is_allowed(url: str) -> None:
    with pytest.raises(LiveTradingNotAllowedError, match="paper trading"):
        broker(FakeTransport(), base_url=url)


def test_live_keys_are_rejected() -> None:
    live = AlpacaTradingCredentials(key_id="AKLIVEKEY", secret_key=SecretStr("s"))

    with pytest.raises(LiveTradingNotAllowedError, match="PK"):
        broker(FakeTransport(), credentials=live)


def test_live_account_is_rejected_before_any_order_is_sent() -> None:
    transport = FakeTransport(response(200, account("LIVE123")))

    with pytest.raises(LiveTradingNotAllowedError, match="PA"):
        broker(transport).submit_order(ENTRY)

    assert transport.routes == ["GET /v2/account"]


def test_guard_is_rechecked_before_every_order() -> None:
    transport = FakeTransport(
        response(200, account()), response(200, order_json(status="new"))
    )
    paper = broker(transport)
    paper.submit_order(ENTRY)
    # Si la configuración cambiase en caliente, la siguiente orden no sale.
    paper._guard._paper_flag = "false"  # noqa: SLF001

    with pytest.raises(LiveTradingNotAllowedError):
        paper.submit_order(ENTRY)

    assert transport.routes == ["GET /v2/account", "POST /v2/orders"]


def test_rejected_credentials_are_fatal() -> None:
    with pytest.raises(LiveTradingNotAllowedError, match="401"):
        broker(FakeTransport(response(401, {"message": "unauthorized"}))).get_account()


def test_factory_reads_paper_settings() -> None:
    local = ApplicationSettings.from_yaml()
    settings = local.model_copy(
        update={
            "alpaca_api_key_id": "PKFROMENV",
            "alpaca_api_secret_key": SecretStr("s"),
            "alpaca_paper": "true",
        }
    )

    assert isinstance(build_alpaca_paper_broker(settings), AlpacaPaperBroker)

    with pytest.raises(LiveTradingNotAllowedError, match="ALPACA_PAPER"):
        build_alpaca_paper_broker(settings.model_copy(update={"alpaca_paper": None}))

    with pytest.raises(LiveTradingNotAllowedError, match="ALPACA_API_KEY_ID"):
        build_alpaca_paper_broker(
            settings.model_copy(update={"alpaca_api_key_id": None})
        )


# --- Envío de órdenes -------------------------------------------------------


def test_submit_entry_limit_order_at_the_open() -> None:
    transport = FakeTransport(
        response(200, account()), response(200, order_json(status="new"))
    )

    state = broker(transport).submit_order(ENTRY)

    method, url, headers, body = transport.calls[1]
    assert (method, url) == ("POST", f"{PAPER_URL}/v2/orders")
    assert headers["APCA-API-KEY-ID"] == CREDENTIALS.key_id
    assert headers["APCA-API-SECRET-KEY"] == "secret"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body or b"") == {
        "client_order_id": ENTRY.client_order_id,
        "symbol": "AAPL",
        "side": "buy",
        "qty": "10",
        "type": "limit",
        "time_in_force": "opg",
        "limit_price": "176.40",
    }
    assert state.status is OrderStatus.SUBMITTED
    assert state.broker_status == "new"
    assert state.limit_price == Decimal("176.4")


def test_submit_oco_exit_payload() -> None:
    assert order_payload(EXIT) == {
        "client_order_id": EXIT.client_order_id,
        "symbol": "AAPL",
        "side": "sell",
        "qty": "10",
        "type": "limit",
        "time_in_force": "gtc",
        "order_class": "oco",
        "take_profit": {"limit_price": "186.85"},
        "stop_loss": {"stop_price": "169.32"},
    }


def test_account_is_verified_only_once() -> None:
    transport = FakeTransport(
        response(200, account()),
        response(200, order_json(status="new")),
        response(200, order_json(EXIT.client_order_id, status="new")),
    )
    paper = broker(transport)

    paper.submit_order(ENTRY)
    paper.submit_order(EXIT)

    assert transport.routes == [
        "GET /v2/account",
        "POST /v2/orders",
        "POST /v2/orders",
    ]


def test_rejected_order_is_not_retried() -> None:
    transport = FakeTransport(
        response(200, account()),
        response(422, {"message": "insufficient buying power"}),
    )

    with pytest.raises(OrderRejectedError, match="insufficient buying power"):
        broker(transport).submit_order(ENTRY)

    assert transport.routes == ["GET /v2/account", "POST /v2/orders"]


def test_network_error_checks_for_the_order_before_resending() -> None:
    """El POST pudo llegar: si la orden existe, no se reenvía."""
    transport = FakeTransport(
        response(200, account()),
        OSError("conexión cortada"),
        response(200, order_json(status="accepted")),
    )

    state = broker(transport).submit_order(ENTRY)

    assert state.client_order_id == ENTRY.client_order_id
    assert transport.routes == [
        "GET /v2/account",
        "POST /v2/orders",
        "GET /v2/orders:by_client_order_id",
    ]
    query = urllib.parse.urlparse(transport.calls[2][1]).query
    assert urllib.parse.parse_qs(query) == {"client_order_id": [ENTRY.client_order_id]}


def test_server_error_resends_only_if_the_order_does_not_exist() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(
        response(200, account()),
        response(503),
        response(404, {"message": "order not found"}),
        response(200, order_json(status="new")),
    )

    state = broker(transport, sleeps=sleeps).submit_order(ENTRY)

    assert state.status is OrderStatus.SUBMITTED
    assert transport.routes == [
        "GET /v2/account",
        "POST /v2/orders",
        "GET /v2/orders:by_client_order_id",
        "POST /v2/orders",
    ]
    assert sleeps == [1.0]


def test_rate_limited_submission_is_retried() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(
        response(200, account()),
        response(429),
        response(200, order_json(status="new")),
    )

    broker(transport, sleeps=sleeps).submit_order(ENTRY)

    assert transport.routes.count("POST /v2/orders") == 2
    assert sleeps == [1.0]


def test_submission_gives_up_after_max_attempts() -> None:
    transport = FakeTransport(
        response(200, account()),
        *[item for _ in range(3) for item in (response(500), response(404))],
    )

    with pytest.raises(BrokerUnavailableError, match="3 intentos"):
        broker(transport).submit_order(ENTRY)

    assert transport.routes.count("POST /v2/orders") == 3


# --- Consultas y cancelación -----------------------------------------------


def test_account_and_positions() -> None:
    transport = FakeTransport(
        response(200, account()),
        response(
            200,
            [
                {
                    "symbol": "AAPL",
                    "qty": "10",
                    "avg_entry_price": "176.12",
                    "market_value": "1790.5",
                }
            ],
        ),
    )
    paper = broker(transport)

    snapshot = paper.get_account()
    (position,) = paper.get_positions()

    assert snapshot.account_number == "PA3XXXXXXXXX"
    assert snapshot.equity == Decimal("100000.5")
    assert not snapshot.trading_blocked
    assert position.quantity == Decimal("10")
    assert position.average_entry_price == Decimal("176.12")


def test_get_order_returns_none_when_unknown() -> None:
    assert broker(FakeTransport(response(404, {}))).get_order("missing") is None


def test_list_open_orders_parses_oco_legs() -> None:
    oco = order_json(
        EXIT.client_order_id,
        status="new",
        side="sell",
        time_in_force="gtc",
        order_class="oco",
        limit_price="186.85",
        legs=[
            order_json(
                "leg-stop",
                status="held",
                side="sell",
                type="stop",
                time_in_force="gtc",
                limit_price=None,
                stop_price="169.32",
            )
        ],
    )
    transport = FakeTransport(response(200, [oco]))

    (state,) = broker(transport).list_open_orders()

    assert state.order_class is OrderClass.OCO
    assert state.legs[0].stop_price == Decimal("169.32")
    assert state.legs[0].status is OrderStatus.SUBMITTED
    params = urllib.parse.parse_qs(urllib.parse.urlparse(transport.calls[0][1]).query)
    assert params["status"] == ["open"]
    assert params["nested"] == ["true"]


def test_cancel_order() -> None:
    transport = FakeTransport(response(200, account()), response(204))

    broker(transport).cancel_order("broker-1")

    assert transport.routes == ["GET /v2/account", "DELETE /v2/orders/broker-1"]


def test_cancel_of_a_filled_order_is_rejected() -> None:
    transport = FakeTransport(
        response(200, account()), response(422, {"message": "order is not cancelable"})
    )

    with pytest.raises(OrderRejectedError, match="not cancelable"):
        broker(transport).cancel_order("broker-1")


# --- Ciclo de vida con el registro local -----------------------------------


def test_order_lifecycle_from_broker_updates() -> None:
    book = OrderBook()

    events = book.apply_all(
        parse_order(order_json(status=status, filled=filled))
        for status, filled in [
            ("pending_new", "0"),
            ("accepted", "0"),
            ("partially_filled", "4"),
            ("filled", "10"),
        ]
    )

    assert [event.status for event in events] == [
        OrderStatus.SUBMITTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
    ]
    filled = book.get(ENTRY.client_order_id)
    assert filled is not None
    assert filled.filled_average_price == Decimal("176.12")


@pytest.mark.parametrize(
    ("status", "expected"),
    [("canceled", OrderStatus.CANCELED), ("expired", OrderStatus.EXPIRED)],
)
def test_unfilled_opening_order_ends_canceled_or_expired(
    status: str, expected: OrderStatus
) -> None:
    """Límite OPG no alcanzado en la subasta (gap mayor que el permitido)."""
    book = OrderBook()

    book.apply_all(
        parse_order(order_json(status=item)) for item in ("accepted", status)
    )

    order = book.get(ENTRY.client_order_id)
    assert order is not None
    assert order.status is expected
    assert order.filled_quantity == 0
