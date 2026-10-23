"""
Adaptador de Alpaca Paper Trading API (`/v2/account`, `/v2/positions`,
`/v2/orders`).

Seguridad (`PaperTradingGuard`): la v2 nunca opera con dinero real. Antes de
crear el adaptador y otra vez antes de cada orden se exige:

1. `ALPACA_PAPER` con el valor exacto `true` (sin distinguir mayúsculas);
   cualquier otro valor, o su ausencia, es fatal.
2. URL base exactamente `https://paper-api.alpaca.markets`.
3. Clave de paper trading (`APCA-API-KEY-ID` con prefijo `PK`).
4. Cuenta de paper (`account_number` con prefijo `PA`), comprobada en la
   primera orden con `GET /v2/account`.

Si algo falla se lanza `LiveTradingNotAllowedError` sin enviar nada.

Envío idempotente: `client_order_id` identifica la orden. Si un POST falla
por red o 5xx, se consulta la orden por `client_order_id` antes de
reintentar, para no duplicarla; un 429 se reintenta tras esperar. Las
lecturas (GET) reintentan con backoff exponencial.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr

from algo_trading.config.settings import ApplicationSettings
from algo_trading.data.providers.alpaca import HttpResponse, RetryPolicy
from algo_trading.execution.broker import AccountSnapshot, BrokerPosition
from algo_trading.execution.orders import (
    OrderClass,
    OrderRequest,
    OrderSide,
    OrderState,
    OrderType,
    TimeInForce,
    normalize_alpaca_status,
)

ALPACA_PAPER_URL = "https://paper-api.alpaca.markets"
PAPER_KEY_PREFIX = "PK"
PAPER_ACCOUNT_PREFIX = "PA"

HttpSend = Callable[[str, str, Mapping[str, str], bytes | None, float], HttpResponse]


class LiveTradingNotAllowedError(RuntimeError):
    """Fatal: la configuración no garantiza una cuenta de paper trading."""


class BrokerError(RuntimeError):
    """Error del broker."""


class OrderRejectedError(BrokerError):
    """El broker rechazó la orden (4xx): no se reintenta."""


class BrokerUnavailableError(BrokerError):
    """Reintentos agotados por cuota, errores 5xx o de red."""


class AlpacaTradingCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: str
    secret_key: SecretStr


class PaperTradingGuard:
    """Comprobaciones previas a cualquier orden; ver el docstring del módulo."""

    def __init__(self, paper_flag: str | None, base_url: str, key_id: str) -> None:
        self._paper_flag = paper_flag
        self._base_url = base_url
        self._key_id = key_id

    def check_configuration(self) -> None:
        if (self._paper_flag or "").strip().lower() != "true":
            raise LiveTradingNotAllowedError(
                "ALPACA_PAPER debe ser exactamente 'true' para enviar órdenes"
            )

        if self._base_url.rstrip("/") != ALPACA_PAPER_URL:
            raise LiveTradingNotAllowedError(
                f"Sólo se admite la URL de paper trading {ALPACA_PAPER_URL}"
            )

        if not self._key_id.startswith(PAPER_KEY_PREFIX):
            raise LiveTradingNotAllowedError(
                "La clave de Alpaca no es de paper trading (prefijo PK)"
            )

    @staticmethod
    def check_account(account: AccountSnapshot) -> None:
        if not account.account_number.startswith(PAPER_ACCOUNT_PREFIX):
            raise LiveTradingNotAllowedError(
                "La cuenta de Alpaca no es de paper trading (prefijo PA)"
            )


def build_alpaca_paper_broker(settings: ApplicationSettings) -> "AlpacaPaperBroker":
    """Broker desde la configuración; falla si no es paper trading."""
    if not settings.alpaca_api_key_id or settings.alpaca_api_secret_key is None:
        raise LiveTradingNotAllowedError(
            "Faltan ALPACA_API_KEY_ID y ALPACA_API_SECRET_KEY en el entorno o en .env"
        )

    broker = settings.broker
    return AlpacaPaperBroker(
        AlpacaTradingCredentials(
            key_id=settings.alpaca_api_key_id,
            secret_key=settings.alpaca_api_secret_key,
        ),
        paper_flag=settings.alpaca_paper,
        base_url=broker.base_url,
        retry=RetryPolicy(max_attempts=broker.max_attempts),
        timeout_seconds=broker.timeout_seconds,
    )


def urllib_send(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    """Transporte por defecto; los errores HTTP se devuelven como respuesta."""
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read(),
            )
    except urllib.error.HTTPError as error:
        return HttpResponse(
            status=error.code,
            headers={key.lower(): value for key, value in error.headers.items()},
            body=error.read(),
        )


class AlpacaPaperBroker:
    """`BrokerAdapter` de Alpaca Paper Trading con la salvaguarda de paper."""

    def __init__(
        self,
        credentials: AlpacaTradingCredentials,
        paper_flag: str | None,
        base_url: str = ALPACA_PAPER_URL,
        retry: RetryPolicy | None = None,
        timeout_seconds: float = 30.0,
        http_send: HttpSend | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._guard = PaperTradingGuard(paper_flag, base_url, credentials.key_id)
        self._guard.check_configuration()
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")
        self._retry = retry or RetryPolicy()
        self._timeout = timeout_seconds
        self._send = http_send or urllib_send
        self._sleep = sleep or time.sleep
        self._account_verified = False

    def get_account(self) -> AccountSnapshot:
        payload = self._get("/v2/account")
        assert isinstance(payload, dict)
        return AccountSnapshot(
            account_number=str(payload["account_number"]),
            status=str(payload["status"]),
            currency=str(payload.get("currency", "USD")),
            cash=Decimal(str(payload["cash"])),
            equity=Decimal(str(payload["equity"])),
            buying_power=Decimal(str(payload["buying_power"])),
            trading_blocked=bool(payload.get("trading_blocked", False)),
        )

    def get_positions(self) -> Sequence[BrokerPosition]:
        payload = self._get("/v2/positions")
        assert isinstance(payload, list)
        return tuple(
            BrokerPosition(
                symbol=str(item["symbol"]),
                quantity=Decimal(str(item["qty"])),
                average_entry_price=Decimal(str(item["avg_entry_price"])),
                market_value=_optional_decimal(item.get("market_value")),
            )
            for item in payload
        )

    def submit_order(self, request: OrderRequest) -> OrderState:
        self._ensure_paper()
        body = json.dumps(order_payload(request)).encode("utf-8")
        last_error = "sin respuesta"

        for attempt in range(self._retry.max_attempts):
            try:
                response = self._send(
                    "POST",
                    f"{self._base_url}/v2/orders",
                    self._headers(json_body=True),
                    body,
                    self._timeout,
                )
            except OSError as error:
                last_error = f"error de red: {error}"
                response = None

            if response is not None and response.status == 200:
                return parse_order(json.loads(response.body))

            if response is not None and response.status == 429:
                last_error = "cuota agotada (429)"
                self._backoff(attempt)
                continue

            if response is not None and response.status < 500:
                raise OrderRejectedError(
                    f"Alpaca rechazó la orden {request.client_order_id} "
                    f"({response.status}): "
                    + response.body.decode("utf-8", errors="replace")[:200]
                )

            if response is not None:
                last_error = f"error del servidor ({response.status})"

            # Red o 5xx: la orden pudo llegar. Se consulta antes de reenviar.
            existing = self.get_order(request.client_order_id)

            if existing is not None:
                return existing

            self._backoff(attempt)

        raise BrokerUnavailableError(
            f"No se pudo enviar {request.client_order_id} tras "
            f"{self._retry.max_attempts} intentos: {last_error}"
        )

    def get_order(self, client_order_id: str) -> OrderState | None:
        payload = self._get(
            "/v2/orders:by_client_order_id",
            {"client_order_id": client_order_id},
            not_found_ok=True,
        )

        if payload is None:
            return None

        assert isinstance(payload, dict)
        return parse_order(payload)

    def list_open_orders(self) -> Sequence[OrderState]:
        payload = self._get(
            "/v2/orders", {"status": "open", "nested": "true", "limit": "500"}
        )
        assert isinstance(payload, list)
        return tuple(parse_order(item) for item in payload)

    def cancel_order(self, broker_order_id: str) -> None:
        self._ensure_paper()
        url = f"{self._base_url}/v2/orders/{urllib.parse.quote(broker_order_id)}"

        for attempt in range(self._retry.max_attempts):
            try:
                response = self._send(
                    "DELETE", url, self._headers(), None, self._timeout
                )
            except OSError:
                self._backoff(attempt)
                continue

            if response.status in (200, 204):
                return

            if response.status == 429 or response.status >= 500:
                self._backoff(attempt)
                continue

            raise OrderRejectedError(
                f"Alpaca no canceló la orden {broker_order_id} ({response.status}): "
                + response.body.decode("utf-8", errors="replace")[:200]
            )

        raise BrokerUnavailableError(f"No se pudo cancelar {broker_order_id}")

    def _ensure_paper(self) -> None:
        self._guard.check_configuration()

        if not self._account_verified:
            self._guard.check_account(self.get_account())
            self._account_verified = True

    def _get(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        not_found_ok: bool = False,
    ) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self._base_url}{path}{query}"
        last_error = "sin respuesta"

        for attempt in range(self._retry.max_attempts):
            try:
                response = self._send("GET", url, self._headers(), None, self._timeout)
            except OSError as error:
                last_error = f"error de red: {error}"
                self._backoff(attempt)
                continue

            if response.status == 200:
                return json.loads(response.body)

            if response.status == 404 and not_found_ok:
                return None

            if response.status == 429 or response.status >= 500:
                last_error = f"HTTP {response.status}"
                self._backoff(attempt)
                continue

            if response.status in (401, 403):
                raise LiveTradingNotAllowedError(
                    f"Alpaca rechazó las credenciales ({response.status})"
                )

            raise BrokerError(
                f"Alpaca respondió {response.status} a GET {path}: "
                + response.body.decode("utf-8", errors="replace")[:200]
            )

        raise BrokerUnavailableError(f"GET {path} falló: {last_error}")

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        headers = {
            "APCA-API-KEY-ID": self._credentials.key_id,
            "APCA-API-SECRET-KEY": self._credentials.secret_key.get_secret_value(),
            "Accept": "application/json",
        }

        if json_body:
            headers["Content-Type"] = "application/json"

        return headers

    def _backoff(self, attempt: int) -> None:
        if attempt + 1 < self._retry.max_attempts:
            self._sleep(self._retry.delay(attempt))


def order_payload(request: OrderRequest) -> dict[str, Any]:
    """Cuerpo JSON de `POST /v2/orders`; precios y cantidades como texto."""
    payload: dict[str, Any] = {
        "client_order_id": request.client_order_id,
        "symbol": request.symbol,
        "side": request.side.value,
        "qty": str(request.quantity),
        "type": request.order_type.value,
        "time_in_force": request.time_in_force.value,
    }

    if request.limit_price is not None:
        payload["limit_price"] = str(request.limit_price)

    if request.stop_price is not None:
        payload["stop_price"] = str(request.stop_price)

    if request.order_class is OrderClass.OCO:
        payload["order_class"] = OrderClass.OCO.value
        payload["take_profit"] = {"limit_price": str(request.take_profit_limit_price)}
        payload["stop_loss"] = {"stop_price": str(request.stop_loss_stop_price)}

    return payload


def parse_order(payload: Mapping[str, Any]) -> OrderState:
    """Orden de Alpaca (con sus patas `legs`) a `OrderState`."""
    try:
        order_class = OrderClass(str(payload.get("order_class") or "simple"))
    except ValueError:  # bracket / oto: no los emite el planificador
        order_class = OrderClass.SIMPLE

    return OrderState(
        broker_order_id=str(payload["id"]),
        client_order_id=str(payload["client_order_id"]),
        symbol=str(payload["symbol"]),
        side=OrderSide(str(payload["side"])),
        order_type=OrderType(str(payload.get("type") or payload["order_type"])),
        time_in_force=TimeInForce(str(payload["time_in_force"])),
        order_class=order_class,
        quantity=Decimal(str(payload["qty"])),
        filled_quantity=Decimal(str(payload.get("filled_qty") or "0")),
        filled_average_price=_optional_decimal(payload.get("filled_avg_price")),
        limit_price=_optional_decimal(payload.get("limit_price")),
        stop_price=_optional_decimal(payload.get("stop_price")),
        status=normalize_alpaca_status(str(payload["status"])),
        broker_status=str(payload["status"]),
        submitted_at=_optional_datetime(payload.get("submitted_at")),
        filled_at=_optional_datetime(payload.get("filled_at")),
        updated_at=_optional_datetime(payload.get("updated_at")),
        legs=tuple(parse_order(leg) for leg in payload.get("legs") or ()),
    )


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None

    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
