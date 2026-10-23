from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal

from algo_trading.indicators.price_series import PriceBar, validate_price_series


class MissingReferenceSeriesError(KeyError):
    """Un filtro pide una serie de referencia que no está en el contexto."""


class SignalContext:
    """
    Series de referencia para los filtros: benchmarks (SPY, QQQ), ETFs
    sectoriales o índices no operables como `^VIX`.

    `until(t)` devuelve una vista que sólo contiene barras con timestamp
    `<= t`. Los generadores pasan esa vista a los filtros al evaluar la
    barra `t`, de modo que un filtro no puede leer datos posteriores.
    """

    def __init__(self, series: Mapping[str, Sequence[PriceBar]] | None = None) -> None:
        bars_by_symbol: dict[str, tuple[PriceBar, ...]] = {}

        for symbol, bars in (series or {}).items():
            validate_price_series(bars)

            if bars and bars[0].symbol != symbol:
                raise ValueError(
                    f"La serie de referencia {symbol} contiene barras de "
                    f"{bars[0].symbol}"
                )

            bars_by_symbol[symbol] = tuple(bars)

        self._bars = bars_by_symbol
        self._timestamps = {
            symbol: tuple(bar.timestamp for bar in bars)
            for symbol, bars in bars_by_symbol.items()
        }

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._bars))

    def until(self, timestamp: datetime) -> "SignalContext":
        """Vista con las barras de cada serie hasta `timestamp` inclusive."""
        view = SignalContext()
        view._bars = {
            symbol: bars[: bisect_right(self._timestamps[symbol], timestamp)]
            for symbol, bars in self._bars.items()
        }
        view._timestamps = {
            symbol: timestamps[: len(view._bars[symbol])]
            for symbol, timestamps in self._timestamps.items()
        }
        return view

    def bars(self, symbol: str) -> tuple[PriceBar, ...]:
        if symbol not in self._bars:
            raise MissingReferenceSeriesError(symbol)

        return self._bars[symbol]

    def close_at(self, symbol: str, timestamp: datetime) -> Decimal | None:
        """Cierre de la barra con exactamente ese timestamp, o None si no existe."""
        bars = self.bars(symbol)
        timestamps = self._timestamps[symbol]
        index = bisect_left(timestamps, timestamp)

        if index < len(timestamps) and timestamps[index] == timestamp:
            return bars[index].close

        return None
