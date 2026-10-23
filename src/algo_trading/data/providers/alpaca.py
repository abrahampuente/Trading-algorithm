"""
Adaptador de Alpaca Market Data API v2 para barras intradía (1m / 5m).

Endpoint: `GET {data_url}/v2/stocks/bars` con las cabeceras
`APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` y los parámetros `symbols`,
`timeframe` (`1Min`, `5Min`), `start`, `end` (RFC 3339), `feed` (`iex`,
`sip`), `adjustment` (`raw`, `split`), `limit` y `page_token` para paginar.

Cuotas y fallos:

- Si una respuesta indica `X-RateLimit-Remaining: 0`, la siguiente petición
  espera a `X-RateLimit-Reset` (época Unix) antes de enviarse.
- 429: espera a `Retry-After` o `X-RateLimit-Reset`, o backoff exponencial si
  no vienen, y reintenta.
- 5xx, timeout o error de red: backoff exponencial (`base * 2^intento`,
  acotado a `max_delay`) y reintento, hasta `max_attempts`.
- Otros 4xx (credenciales, parámetros): fallo inmediato, sin reintentos.

Agotados los reintentos se lanza `AlpacaUnavailableError`; quien consume el
proveedor decide el fallback (la resolución intrabarra vuelve a la política
conservadora de la v1). Usa sólo la librería estándar (`urllib`); el
transporte HTTP es inyectable para los tests.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from algo_trading.data.market_bar import MarketBar
from algo_trading.data.market_calendar import TradingCalendar
from algo_trading.data.providers.dto import (
    CorporateActionRequest,
    DataRequest,
    DataSnapshot,
    MarketDataSnapshot,
    Timeframe,
)
from algo_trading.data.providers.protocol import MarketDataProvider
from algo_trading.data.providers.snapshot_checksum import (
    calculate_snapshot_checksum,
)
from algo_trading.domain.corporate_actions.dto import (
    CorporateDividend,
    CorporateSplit,
)

ALPACA_DATA_URL = "https://data.alpaca.markets"
ALPACA_SOURCE_PREFIX = "alpaca"
PRICE_QUANTUM = Decimal("0.00000001")
MAX_PAGE_LIMIT = 10_000
ALPACA_TIMEFRAMES = {
    Timeframe.ONE_MINUTE.value: "1Min",
    Timeframe.FIVE_MINUTES.value: "5Min",
}


class AlpacaFeed(StrEnum):
    IEX = "iex"
    SIP = "sip"


class AlpacaAdjustment(StrEnum):
    RAW = "raw"
    SPLIT = "split"


class AlpacaDataError(RuntimeError):
    """Error de la API de datos de Alpaca."""


class AlpacaRequestError(AlpacaDataError):
    """Petición rechazada sin reintento (credenciales, parámetros: 4xx)."""


class AlpacaAuthenticationError(AlpacaRequestError):
    """Credenciales rechazadas (401 / 403): ninguna petición va a funcionar."""


class AlpacaUnavailableError(AlpacaDataError):
    """Reintentos agotados por cuota (429), errores 5xx o de red."""


class AlpacaRequestStats(BaseModel):
    """
    Telemetría de las peticiones HTTP de un proveedor.

    `requests` cuenta cada intento (incluidos los reintentos); la latencia es
    la de cada intento con respuesta. `rate_limit_*` es la cuota que informa
    Alpaca (`X-RateLimit-Limit` / `-Remaining`): el mínimo de `remaining`
    visto indica cuánto se acercó la ejecución al límite por minuto.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    requests: int = 0
    successful: int = 0
    rate_limited: int = 0
    server_errors: int = 0
    network_errors: int = 0
    client_errors: int = 0
    total_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    slept_seconds: float = 0.0
    rate_limit_limit: int | None = None
    min_rate_limit_remaining: int | None = None

    @property
    def responses(self) -> int:
        return self.requests - self.network_errors

    @property
    def average_latency_ms(self) -> float | None:
        return self.total_latency_ms / self.responses if self.responses else None


class AlpacaCredentials(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key_id: str = Field(min_length=1)
    secret_key: SecretStr


class RetryPolicy(BaseModel):
    """Backoff exponencial determinista: `base * 2^intento`, acotado a `max`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(default=5, ge=1)
    base_delay_seconds: float = Field(default=1.0, ge=0)
    max_delay_seconds: float = Field(default=60.0, ge=0)

    def delay(self, attempt: int) -> float:
        """Espera tras el intento `attempt` (0 = primero) fallido."""
        delay: float = self.base_delay_seconds * 2**attempt
        return min(delay, self.max_delay_seconds)


class HttpResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int
    headers: dict[str, str]
    body: bytes


HttpGet = Callable[[str, Mapping[str, str], float], HttpResponse]


class RejectedAlpacaBar(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    timestamp: datetime
    reason: str


def urllib_get(url: str, headers: Mapping[str, str], timeout: float) -> HttpResponse:
    """Transporte por defecto; los errores HTTP se devuelven como respuesta."""
    request = urllib.request.Request(url, headers=dict(headers), method="GET")

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


class AlpacaMarketDataProvider:
    """
    `MarketDataProvider` de barras intradía de Alpaca.

    - `source = alpaca-<feed>` (`alpaca-iex`, `alpaca-sip`), para no mezclar
      feeds en `market_bars_raw`.
    - `timeframe`: `1m` o `5m`. El timestamp de cada barra es su inicio, en
      UTC. Incluye pre y post-market si el rango los cubre; quien necesite la
      sesión regular la recorta con el calendario.
    - `adjustment = raw` por defecto (capa raw inmutable). `split` da precios
      ajustados por todos los splits conocidos hasta hoy, la misma escala
      que `market_bars_adjusted` (`splits-v1`).
    - No sirve acciones corporativas: los splits y dividendos llegan con el
      proveedor diario; `get_splits` y `get_dividends` devuelven vacío.
    - Precios y volúmenes con 8 decimales (escala de MySQL), leídos del JSON
      como `Decimal` sin pasar por `float`. Las barras con OHLC inconsistente
      se descartan y quedan en `rejected_bars`.
    """

    def __init__(
        self,
        credentials: AlpacaCredentials,
        feed: AlpacaFeed = AlpacaFeed.IEX,
        adjustment: AlpacaAdjustment = AlpacaAdjustment.RAW,
        data_url: str = ALPACA_DATA_URL,
        retry: RetryPolicy | None = None,
        timeout_seconds: float = 30.0,
        page_limit: int = MAX_PAGE_LIMIT,
        http_get: HttpGet | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        timer: Callable[[], float] | None = None,
    ) -> None:
        if not 1 <= page_limit <= MAX_PAGE_LIMIT:
            raise ValueError(f"page_limit debe estar entre 1 y {MAX_PAGE_LIMIT}")

        self._credentials = credentials
        self._feed = feed
        self._adjustment = adjustment
        self._data_url = data_url.rstrip("/")
        self._retry = retry or RetryPolicy()
        self._timeout = timeout_seconds
        self._page_limit = page_limit
        self._http_get = http_get or urllib_get
        self._sleep = sleep or time.sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timer = timer or time.perf_counter
        self._quota_reset: float | None = None
        self._rejected: list[RejectedAlpacaBar] = []
        self._stats = AlpacaRequestStats()

    @property
    def source(self) -> str:
        return f"{ALPACA_SOURCE_PREFIX}-{self._feed}"

    @property
    def rejected_bars(self) -> tuple[RejectedAlpacaBar, ...]:
        return tuple(self._rejected)

    @property
    def stats(self) -> AlpacaRequestStats:
        return self._stats

    def get_bars(self, request: DataRequest) -> Sequence[MarketBar]:
        if request.source != self.source:
            raise ValueError(f"AlpacaMarketDataProvider sirve el source {self.source}")

        if request.timeframe not in ALPACA_TIMEFRAMES:
            raise ValueError(
                "AlpacaMarketDataProvider sólo soporta los timeframes "
                + ", ".join(ALPACA_TIMEFRAMES)
            )

        bars: list[MarketBar] = []
        page_token: str | None = None

        while True:
            payload = self._request_json(
                "/v2/stocks/bars",
                {
                    "symbols": ",".join(sorted(request.symbols)),
                    "timeframe": ALPACA_TIMEFRAMES[request.timeframe],
                    "start": _rfc3339(request.start),
                    "end": _rfc3339(request.end),
                    "feed": self._feed.value,
                    "adjustment": self._adjustment.value,
                    "limit": str(self._page_limit),
                    "sort": "asc",
                    **({"page_token": page_token} if page_token else {}),
                },
            )

            for symbol, rows in sorted((payload.get("bars") or {}).items()):
                bars.extend(
                    bar
                    for row in rows
                    if (bar := self._to_bar(symbol, row, request.timeframe)) is not None
                )

            page_token = payload.get("next_page_token")

            if not page_token:
                break

        return tuple(sorted(bars, key=lambda bar: (bar.symbol, bar.timestamp)))

    def get_splits(self, request: CorporateActionRequest) -> Sequence[CorporateSplit]:
        return ()

    def get_dividends(
        self, request: CorporateActionRequest
    ) -> Sequence[CorporateDividend]:
        return ()

    def get_snapshot(
        self,
        data_request: DataRequest,
        corporate_action_request: CorporateActionRequest,
    ) -> MarketDataSnapshot:
        bars = tuple(self.get_bars(data_request))

        return MarketDataSnapshot(
            metadata=DataSnapshot(
                snapshot_id=build_snapshot_id(data_request),
                source=self.source,
                retrieved_at=self._clock(),
                timeframe=data_request.timeframe,
                bars_count=len(bars),
                splits_count=0,
                dividends_count=0,
                checksum=calculate_snapshot_checksum(
                    bars=bars, splits=(), dividends=()
                ),
            ),
            bars=bars,
            splits=(),
            dividends=(),
        )

    def _request_json(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        url = f"{self._data_url}{path}?{urllib.parse.urlencode(params)}"
        headers = {
            "APCA-API-KEY-ID": self._credentials.key_id,
            "APCA-API-SECRET-KEY": self._credentials.secret_key.get_secret_value(),
            "Accept": "application/json",
        }
        last_error = "sin respuesta"

        for attempt in range(self._retry.max_attempts):
            self._wait_for_quota()

            started = self._timer()

            try:
                response = self._http_get(url, headers, self._timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                self._count(network_errors=1)
                last_error = f"error de red: {error}"
                self._backoff(attempt)
                continue

            self._record_response(response, (self._timer() - started) * 1000)
            self._remember_quota(response.headers)

            if response.status == 200:
                payload: dict[str, Any] = json.loads(
                    response.body, parse_float=Decimal, parse_int=Decimal
                )
                return payload

            if response.status == 429:
                last_error = "cuota agotada (429)"
                self._sleep_until_allowed(response.headers, attempt)
                continue

            if response.status >= 500:
                last_error = f"error del servidor ({response.status})"
                self._backoff(attempt)
                continue

            error_type = (
                AlpacaAuthenticationError
                if response.status in (401, 403)
                else AlpacaRequestError
            )
            raise error_type(
                f"Alpaca rechazó la petición ({response.status}): "
                + response.body.decode("utf-8", errors="replace")[:200]
            )

        raise AlpacaUnavailableError(
            f"Alpaca no disponible tras {self._retry.max_attempts} intentos: "
            f"{last_error}"
        )

    def _count(self, **increments: int) -> None:
        self._stats = self._stats.model_copy(
            update={
                "requests": self._stats.requests + 1,
                **{
                    name: getattr(self._stats, name) + value
                    for name, value in increments.items()
                },
            }
        )

    def _record_response(self, response: HttpResponse, latency_ms: float) -> None:
        status = response.status
        self._count(
            successful=int(status == 200),
            rate_limited=int(status == 429),
            server_errors=int(status >= 500),
            client_errors=int(400 <= status < 500 and status != 429),
        )
        stats = self._stats
        limit = _header_int(response.headers, "x-ratelimit-limit")
        remaining = _header_int(response.headers, "x-ratelimit-remaining")
        min_remaining = stats.min_rate_limit_remaining

        if remaining is not None:
            min_remaining = (
                remaining if min_remaining is None else min(min_remaining, remaining)
            )

        self._stats = stats.model_copy(
            update={
                "total_latency_ms": stats.total_latency_ms + latency_ms,
                "max_latency_ms": max(stats.max_latency_ms, latency_ms),
                "rate_limit_limit": limit or stats.rate_limit_limit,
                "min_rate_limit_remaining": min_remaining,
            }
        )

    def _pause(self, seconds: float) -> None:
        self._stats = self._stats.model_copy(
            update={"slept_seconds": self._stats.slept_seconds + seconds}
        )
        self._sleep(seconds)

    def _backoff(self, attempt: int) -> None:
        if attempt + 1 < self._retry.max_attempts:
            self._pause(self._retry.delay(attempt))

    def _sleep_until_allowed(self, headers: Mapping[str, str], attempt: int) -> None:
        if attempt + 1 >= self._retry.max_attempts:
            return

        retry_after = headers.get("retry-after")

        if retry_after is not None and retry_after.isdigit():
            self._pause(min(float(retry_after), self._retry.max_delay_seconds))
            return

        if self._quota_reset is not None:
            self._wait_for_quota()
            return

        self._pause(self._retry.delay(attempt))

    def _remember_quota(self, headers: Mapping[str, str]) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        self._quota_reset = (
            float(reset)
            if remaining is not None
            and remaining.strip() == "0"
            and reset is not None
            and reset.isdigit()
            else None
        )

    def _wait_for_quota(self) -> None:
        if self._quota_reset is None:
            return

        wait = self._quota_reset - self._clock().timestamp()
        self._quota_reset = None

        if wait > 0:
            self._pause(min(wait, self._retry.max_delay_seconds))

    def _to_bar(
        self, symbol: str, row: Mapping[str, Any], timeframe: str
    ) -> MarketBar | None:
        timestamp = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00"))

        try:
            return MarketBar(
                symbol=symbol,
                timestamp=timestamp.astimezone(UTC),
                timeframe=timeframe,
                source=self.source,
                open=_quantize(row["o"]),
                high=_quantize(row["h"]),
                low=_quantize(row["l"]),
                close=_quantize(row["c"]),
                volume=_quantize(row["v"]),
            )
        except (KeyError, ValidationError) as error:
            self._rejected.append(
                RejectedAlpacaBar(symbol=symbol, timestamp=timestamp, reason=str(error))
            )
            return None


def build_snapshot_id(request: DataRequest) -> str:
    """`alpaca-<feed>:<tf>:<símbolos>:<inicio>:<fin>` con timestamps UTC."""
    return (
        f"{request.source}:{request.timeframe}:{','.join(sorted(request.symbols))}:"
        f"{_rfc3339(request.start)}:{_rfc3339(request.end)}"
    )


class AlpacaSessionBarSource:
    """
    Barras de 1 minuto de la sesión regular de un símbolo, bajo demanda.

    Pide `[apertura, cierre)` de la sesión según el calendario (incluidos los
    cierres tempranos) y descarta lo que quede fuera. Use un proveedor con
    `adjustment = split` para que los precios estén en la escala de las
    barras diarias ajustadas (`splits-v1`).
    """

    def __init__(
        self,
        provider: AlpacaMarketDataProvider,
        calendar: TradingCalendar,
    ) -> None:
        self._provider = provider
        self._calendar = calendar

    @property
    def provider(self) -> AlpacaMarketDataProvider:
        return self._provider

    def get_session_bars(self, symbol: str, session_date: date) -> Sequence[MarketBar]:
        sessions = self._calendar.sessions(session_date, session_date)

        if not sessions:
            return ()

        session = sessions[0]
        bars = self._provider.get_bars(
            DataRequest(
                symbols=(symbol,),
                start=session.market_open,
                end=session.market_close,
                timeframe=Timeframe.ONE_MINUTE.value,
                source=self._provider.source,
            )
        )
        return tuple(
            bar
            for bar in bars
            if session.market_open <= bar.timestamp < session.market_close
        )


def _rfc3339(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    value = headers.get(name, "").strip()
    return int(value) if value.isdigit() else None


def _quantize(value: Any) -> Decimal:
    return Decimal(value).quantize(PRICE_QUANTUM)


_PROVIDER_PROTOCOL_CHECK: type[MarketDataProvider] = AlpacaMarketDataProvider
