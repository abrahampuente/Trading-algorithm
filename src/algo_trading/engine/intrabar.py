"""
Resolución intrabarra bajo demanda de la precedencia SL / TP.

Con barras diarias, una vela que toca el SL y el TP no dice cuál se alcanzó
primero; la v1 aplica siempre el SL. `IntrabarResolver` sólo consulta datos
intradía en ese caso:

1. La vela no toca ningún nivel: sin salida y sin descarga.
2. Toca sólo uno: se ejecuta ese nivel con las reglas de la v1
   (`evaluate_bar_exit`), sin descarga.
3. Toca ambos y la apertura ya está más allá de un nivel: ese nivel es el
   primero (`OPENING_GAP`), sin descarga. Con gap bajista, SL a la apertura;
   con gap alcista, TP al nivel (no se asume mejora de precio, como en la v1).
4. Toca ambos dentro de la sesión: descarga las barras de 1 minuto de esa
   sesión (una vez por símbolo y sesión) y recorre la trayectoria en orden:

   - El primer minuto que toca el SL decide `STOP_LOSS` a `min(apertura del
     minuto, SL)`.
   - Si el primer nivel tocado es el TP, sale por `TAKE_PROFIT` al nivel sólo
     si la trayectoria muestra también el SL más tarde; si el SL no aparece
     en los datos intradía (feed parcial como IEX), no se sabe cuándo se tocó
     y se aplica el fallback.
   - Un minuto que toca ambos niveles se resuelve como SL (`SAME_MINUTE`).

Fallback de la v1 (SL con prioridad) cuando no hay fuente, la fuente falla,
no hay barras de la sesión, las barras salen del rango diario más allá de la
tolerancia (escala distinta, p. ej. precios sin ajustar por splits) o la
trayectoria no muestra el orden. Cada vela con ambos niveles deja un
`IntrabarResolutionRecord` con el resultado y su motivo.

Las barras intradía deben estar en la escala de las barras diarias del
backtest (ajustadas por splits).
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from algo_trading.execution.exit_rules import (
    ExitDecision,
    ExitReason,
    evaluate_bar_exit,
)
from algo_trading.portfolio.exit_levels import ExitLevels

DEFAULT_PRICE_TOLERANCE = Decimal("0.01")


class IntradayBar(Protocol):
    @property
    def timestamp(self) -> datetime: ...

    @property
    def open(self) -> Decimal: ...

    @property
    def high(self) -> Decimal: ...

    @property
    def low(self) -> Decimal: ...


class IntradayBarSource(Protocol):
    """Barras de 1 minuto de la sesión regular de un símbolo."""

    def get_session_bars(
        self, symbol: str, session_date: date
    ) -> Sequence[IntradayBar]: ...


class IntrabarResolution(StrEnum):
    OPENING_GAP = "OPENING_GAP"
    INTRADAY_PATH = "INTRADAY_PATH"
    SAME_MINUTE = "SAME_MINUTE"
    FALLBACK_NO_SOURCE = "FALLBACK_NO_SOURCE"
    FALLBACK_SOURCE_ERROR = "FALLBACK_SOURCE_ERROR"
    FALLBACK_NO_DATA = "FALLBACK_NO_DATA"
    FALLBACK_INCONSISTENT = "FALLBACK_INCONSISTENT"
    FALLBACK_UNRESOLVED = "FALLBACK_UNRESOLVED"

    @property
    def is_fallback(self) -> bool:
        return self.value.startswith("FALLBACK_")


class IntrabarResolutionRecord(BaseModel):
    """Vela diaria que tocó SL y TP y cómo se resolvió."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    session_timestamp: datetime
    stop_loss: Decimal
    take_profit: Decimal
    resolution: IntrabarResolution
    exit_reason: ExitReason
    exit_price: Decimal
    first_touch_timestamp: datetime | None = None
    intraday_bars: int = Field(default=0, ge=0)
    error: str | None = None


class IntrabarResolver:
    """
    Decide la salida SL / TP de una posición Long en una vela diaria.

    `source = None` equivale a la política de la v1 en todos los casos, pero
    registra las velas con ambos niveles (`FALLBACK_NO_SOURCE`).
    """

    def __init__(
        self,
        source: IntradayBarSource | None,
        price_tolerance: Decimal = DEFAULT_PRICE_TOLERANCE,
    ) -> None:
        if price_tolerance < 0:
            raise ValueError("price_tolerance no puede ser negativa")

        self._source = source
        self._tolerance = price_tolerance
        self._cache: dict[tuple[str, date], tuple[IntradayBar, ...] | str] = {}

    @property
    def requested_sessions(self) -> int:
        """Sesiones intradía pedidas a la fuente (cada una sólo una vez)."""
        return len(self._cache)

    def resolve(
        self,
        symbol: str,
        session_timestamp: datetime,
        open_price: Decimal,
        high: Decimal,
        low: Decimal,
        levels: ExitLevels,
    ) -> tuple[ExitDecision | None, IntrabarResolutionRecord | None]:
        if not (low <= levels.stop_loss and high >= levels.take_profit):
            return evaluate_bar_exit(open_price, high, low, levels), None

        def record(
            resolution: IntrabarResolution,
            decision: ExitDecision,
            first_touch: datetime | None = None,
            bars: int = 0,
            error: str | None = None,
        ) -> tuple[ExitDecision, IntrabarResolutionRecord]:
            return decision, IntrabarResolutionRecord(
                symbol=symbol,
                session_timestamp=session_timestamp,
                stop_loss=levels.stop_loss,
                take_profit=levels.take_profit,
                resolution=resolution,
                exit_reason=decision.reason,
                exit_price=decision.theoretical_price,
                first_touch_timestamp=first_touch,
                intraday_bars=bars,
                error=error,
            )

        stop_loss = ExitDecision(
            reason=ExitReason.STOP_LOSS,
            theoretical_price=min(open_price, levels.stop_loss),
        )

        if open_price <= levels.stop_loss:
            return record(IntrabarResolution.OPENING_GAP, stop_loss)

        if open_price >= levels.take_profit:
            return record(
                IntrabarResolution.OPENING_GAP,
                ExitDecision(
                    reason=ExitReason.TAKE_PROFIT,
                    theoretical_price=levels.take_profit,
                ),
            )

        if self._source is None:
            return record(IntrabarResolution.FALLBACK_NO_SOURCE, stop_loss)

        bars = self._session_bars(symbol, session_timestamp.date())

        if isinstance(bars, str):
            return record(
                IntrabarResolution.FALLBACK_SOURCE_ERROR, stop_loss, error=bars
            )

        if not bars:
            return record(IntrabarResolution.FALLBACK_NO_DATA, stop_loss)

        if max(bar.high for bar in bars) > high * (1 + self._tolerance) or min(
            bar.low for bar in bars
        ) < low * (1 - self._tolerance):
            return record(
                IntrabarResolution.FALLBACK_INCONSISTENT, stop_loss, bars=len(bars)
            )

        take_profit_at: datetime | None = None

        for bar in bars:
            hits_stop = bar.low <= levels.stop_loss
            hits_target = bar.high >= levels.take_profit

            if hits_stop and take_profit_at is None:
                return record(
                    (
                        IntrabarResolution.SAME_MINUTE
                        if hits_target
                        else IntrabarResolution.INTRADAY_PATH
                    ),
                    ExitDecision(
                        reason=ExitReason.STOP_LOSS,
                        theoretical_price=min(bar.open, levels.stop_loss),
                    ),
                    first_touch=bar.timestamp,
                    bars=len(bars),
                )

            if hits_stop and take_profit_at is not None:
                return record(
                    IntrabarResolution.INTRADAY_PATH,
                    ExitDecision(
                        reason=ExitReason.TAKE_PROFIT,
                        theoretical_price=levels.take_profit,
                    ),
                    first_touch=take_profit_at,
                    bars=len(bars),
                )

            if hits_target and take_profit_at is None:
                take_profit_at = bar.timestamp

        return record(
            IntrabarResolution.FALLBACK_UNRESOLVED,
            stop_loss,
            first_touch=take_profit_at,
            bars=len(bars),
        )

    def _session_bars(
        self, symbol: str, session_date: date
    ) -> tuple[IntradayBar, ...] | str:
        """Barras ordenadas de la sesión (en caché), o el error de la fuente."""
        key = (symbol, session_date)

        if key not in self._cache:
            assert self._source is not None

            try:
                bars = self._source.get_session_bars(symbol, session_date)
                self._cache[key] = tuple(
                    sorted(
                        (bar for bar in bars if bar.timestamp.date() == session_date),
                        key=lambda bar: bar.timestamp,
                    )
                )
            except Exception as error:  # fallback prudente ante cualquier fallo
                self._cache[key] = f"{type(error).__name__}: {error}"

        return self._cache[key]
