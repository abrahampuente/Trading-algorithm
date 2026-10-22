import math
import warnings
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from algo_trading.data.adjustments.split_adjustment import SplitAdjustmentCalculator
from algo_trading.data.market_bar import MarketBar
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

YFINANCE_SOURCE = "yfinance"
PRICE_QUANTUM = Decimal("0.00000001")
DEFAULT_TIMEOUT_SECONDS = 30.0


class YahooDailyRow(BaseModel):
    """Fila diaria tal como la entrega Yahoo: precios ya ajustados por splits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class YahooCorporateEvent(BaseModel):
    """Split (ratio) o dividendo (importe ajustado por splits) de Yahoo."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ex_date: date
    value: Decimal


class YahooHistory(BaseModel):
    """
    Historial de un símbolo.

    `rows` cubre la ventana solicitada; `splits` y `dividends` cubren todo el
    historial disponible, porque Yahoo ajusta los precios por todos los
    splits conocidos hasta hoy, incluso los posteriores a la ventana.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[YahooDailyRow, ...]
    splits: tuple[YahooCorporateEvent, ...]
    dividends: tuple[YahooCorporateEvent, ...]


class RejectedYahooRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    session_date: date
    reason: str


HistoryFetcher = Callable[[str, date, date], YahooHistory]


class YFinanceProvider:
    """
    Adaptador de Yahoo Finance (vía `yfinance`) para datos diarios.

    Yahoo entrega OHLCV y dividendos ya ajustados por splits. Para mantener
    `market_bars_raw` inmutable y sin ajustar, el adaptador reconstruye los
    valores raw deshaciendo los splits posteriores a cada barra:

    - `precio_raw = precio_yahoo / factor`
    - `volumen_raw = volumen_yahoo * factor`
    - `dividendo_raw = dividendo_yahoo / factor`

    donde `factor` es el factor acumulado de `SplitAdjustmentCalculator`.

    Convenciones:

    - Sólo timeframe diario (`1d`).
    - `timestamp` = fecha de sesión de la bolsa a las 00:00 UTC.
    - Precios y volúmenes redondeados a 8 decimales, la escala de MySQL.
    - Las filas con valores ausentes u OHLC inconsistente se descartan y
      quedan en `rejected_rows` para su revisión.
    """

    def __init__(
        self,
        fetcher: HistoryFetcher | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._fetcher = fetcher or fetch_yahoo_history
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache: dict[tuple[str, date, date], YahooHistory] = {}
        self._rejected_rows: list[RejectedYahooRow] = []

    @property
    def rejected_rows(self) -> tuple[RejectedYahooRow, ...]:
        return tuple(self._rejected_rows)

    def get_bars(self, request: DataRequest) -> Sequence[MarketBar]:
        self._validate_source(request.source)

        if request.timeframe != Timeframe.DAILY:
            raise ValueError("YFinanceProvider sólo soporta el timeframe 1d")

        bars: list[MarketBar] = []

        for symbol in request.symbols:
            history = self._history(
                symbol,
                request.start.date(),
                request.end.date(),
            )
            splits = self._all_splits(symbol, history)

            for row in history.rows:
                bar = self._to_raw_bar(symbol, row, splits)

                if bar is not None and request.start <= bar.timestamp <= request.end:
                    bars.append(bar)

        return tuple(bars)

    def get_splits(self, request: CorporateActionRequest) -> Sequence[CorporateSplit]:
        self._validate_source(request.source)

        return tuple(
            split
            for symbol in request.symbols
            for split in self._all_splits(
                symbol,
                self._history(symbol, request.start, request.end),
            )
            if request.start <= split.ex_date <= request.end
        )

    def get_dividends(
        self, request: CorporateActionRequest
    ) -> Sequence[CorporateDividend]:
        self._validate_source(request.source)
        dividends: list[CorporateDividend] = []

        for symbol in request.symbols:
            history = self._history(symbol, request.start, request.end)
            splits = self._all_splits(symbol, history)

            for event in history.dividends:
                if not request.start <= event.ex_date <= request.end:
                    continue

                if event.value <= 0:
                    continue

                factor = SplitAdjustmentCalculator.cumulative_factor_for_bar(
                    bar_date=event.ex_date,
                    splits=splits,
                )
                dividends.append(
                    CorporateDividend(
                        symbol=symbol,
                        ex_date=event.ex_date,
                        amount=_quantize(event.value / factor),
                        source=YFINANCE_SOURCE,
                    )
                )

        return tuple(dividends)

    def get_snapshot(
        self,
        data_request: DataRequest,
        corporate_action_request: CorporateActionRequest,
    ) -> MarketDataSnapshot:
        bars = tuple(self.get_bars(data_request))
        splits = tuple(self.get_splits(corporate_action_request))
        dividends = tuple(self.get_dividends(corporate_action_request))

        return MarketDataSnapshot(
            metadata=DataSnapshot(
                snapshot_id=build_snapshot_id(data_request),
                source=YFINANCE_SOURCE,
                retrieved_at=self._clock(),
                timeframe=data_request.timeframe,
                bars_count=len(bars),
                splits_count=len(splits),
                dividends_count=len(dividends),
                checksum=calculate_snapshot_checksum(
                    bars=bars,
                    splits=splits,
                    dividends=dividends,
                ),
            ),
            bars=bars,
            splits=splits,
            dividends=dividends,
        )

    def _history(self, symbol: str, start: date, end: date) -> YahooHistory:
        key = (symbol, start, end)

        if key not in self._cache:
            self._cache[key] = self._fetcher(symbol, start, end)

        return self._cache[key]

    @staticmethod
    def _all_splits(symbol: str, history: YahooHistory) -> tuple[CorporateSplit, ...]:
        return tuple(
            CorporateSplit(
                symbol=symbol,
                ex_date=event.ex_date,
                ratio=event.value,
                source=YFINANCE_SOURCE,
            )
            for event in history.splits
            if event.value > 0 and event.value != 1
        )

    def _to_raw_bar(
        self,
        symbol: str,
        row: YahooDailyRow,
        splits: Sequence[CorporateSplit],
    ) -> MarketBar | None:
        factor = SplitAdjustmentCalculator.cumulative_factor_for_bar(
            bar_date=row.session_date,
            splits=splits,
        )

        try:
            return MarketBar(
                symbol=symbol,
                timestamp=datetime(
                    row.session_date.year,
                    row.session_date.month,
                    row.session_date.day,
                    tzinfo=UTC,
                ),
                timeframe=Timeframe.DAILY.value,
                source=YFINANCE_SOURCE,
                open=_quantize(row.open / factor),
                high=_quantize(row.high / factor),
                low=_quantize(row.low / factor),
                close=_quantize(row.close / factor),
                volume=_quantize(row.volume * factor),
            )
        except ValidationError as error:
            self._rejected_rows.append(
                RejectedYahooRow(
                    symbol=symbol,
                    session_date=row.session_date,
                    reason=error.errors()[0]["msg"],
                )
            )
            return None

    @staticmethod
    def _validate_source(source: str) -> None:
        if source != YFINANCE_SOURCE:
            raise ValueError(f"YFinanceProvider sólo sirve el source {YFINANCE_SOURCE}")


def build_snapshot_id(request: DataRequest) -> str:
    """`yfinance:1d:<símbolos>:<inicio>:<fin>`, estable para la misma solicitud."""
    return (
        f"{request.source}:{request.timeframe}:{','.join(sorted(request.symbols))}:"
        f"{request.start.date().isoformat()}:{request.end.date().isoformat()}"
    )


def fetch_yahoo_history(
    symbol: str,
    start: date,
    end: date,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> YahooHistory:
    """
    Descarga de Yahoo el historial diario `[start, end]` de un símbolo.

    Es la única función que toca `yfinance`/`pandas` (sin tipos): convierte
    sus estructuras a DTOs tipados. `end` es inclusivo; yfinance lo trata
    como exclusivo, por eso se pide hasta `end + 1 día`. No reintenta: la CLI
    aísla el fallo por símbolo y una nueva ejecución reanuda la ingesta.
    """
    import yfinance

    ticker: Any = yfinance.Ticker(symbol)

    with warnings.catch_warnings():
        # yfinance usa APIs de pandas en deprecación; no afecta a los datos.
        warnings.filterwarnings("ignore", module=r"yfinance(\..*)?")
        frame: Any = ticker.history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
            actions=False,
            timeout=timeout_seconds,
            raise_errors=True,
        )
        # Acciones de todo el historial, con timeout explícito (las
        # propiedades `ticker.splits`/`ticker.dividends` no lo admiten).
        actions: Any = ticker.history(
            period="max",
            interval="1d",
            auto_adjust=False,
            actions=True,
            timeout=timeout_seconds,
            raise_errors=True,
        )

    rows: list[YahooDailyRow] = []

    for index, values in frame.iterrows():
        numbers = [
            values["Open"],
            values["High"],
            values["Low"],
            values["Close"],
            values["Volume"],
        ]

        if any(_is_missing(number) for number in numbers):
            continue

        open_price, high, low, close, volume = (_to_decimal(n) for n in numbers)
        rows.append(
            YahooDailyRow(
                session_date=index.date(),
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )

    return YahooHistory(
        rows=tuple(rows),
        splits=_series_to_events(actions["Stock Splits"]),
        dividends=_series_to_events(actions["Dividends"]),
    )


def _series_to_events(series: Any) -> tuple[YahooCorporateEvent, ...]:
    """Convierte una columna de acciones de Yahoo; 0 significa sin evento."""
    return tuple(
        YahooCorporateEvent(ex_date=index.date(), value=_to_decimal(value))
        for index, value in series.items()
        if not _is_missing(value) and float(value) != 0
    )


def _to_decimal(value: Any) -> Decimal:
    return Decimal(str(float(value)))


def _is_missing(value: Any) -> bool:
    return value is None or math.isnan(float(value))


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(PRICE_QUANTUM)


_PROVIDER_PROTOCOL_CHECK: type[MarketDataProvider] = YFinanceProvider
