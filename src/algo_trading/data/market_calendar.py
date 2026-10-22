import warnings
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

DEFAULT_EXCHANGE = "NYSE"


class TradingSession(BaseModel):
    """Sesión de mercado con apertura y cierre en UTC."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_date: date
    market_open: datetime
    market_close: datetime
    is_early_close: bool


class TradingCalendar(Protocol):
    @property
    def name(self) -> str: ...

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        """Sesiones con fecha en el intervalo inclusivo [start, end]."""
        ...


class ExchangeCalendar:
    """
    Calendario de una bolsa basado en `pandas-market-calendars`.

    Por defecto NYSE; NASDAQ comparte festivos y horario regular. Detecta
    festivos (no son sesiones), cierres excepcionales y cierres tempranos
    (`is_early_close`). Es la única clase que toca `pandas-market-calendars`.
    """

    def __init__(self, exchange: str = DEFAULT_EXCHANGE) -> None:
        import pandas_market_calendars

        self._exchange = exchange
        self._calendar: Any = pandas_market_calendars.get_calendar(exchange)

    @property
    def name(self) -> str:
        return self._exchange

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        if end < start:
            raise ValueError("end no puede ser anterior a start")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            schedule: Any = self._calendar.schedule(
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
            early_closes = {
                timestamp.date()
                for timestamp in self._calendar.early_closes(schedule).index
            }

        return tuple(
            TradingSession(
                session_date=index.date(),
                market_open=row["market_open"].to_pydatetime(),
                market_close=row["market_close"].to_pydatetime(),
                is_early_close=index.date() in early_closes,
            )
            for index, row in schedule.iterrows()
        )


class SessionCoverage(BaseModel):
    """
    Cobertura de la serie de un símbolo frente al calendario.

    Se evalúa entre la primera y la última barra del símbolo.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    first_date: date | None
    last_date: date | None
    expected_sessions: int
    missing_sessions: tuple[date, ...]
    non_session_dates: tuple[date, ...]

    @property
    def is_complete(self) -> bool:
        return not self.missing_sessions and not self.non_session_dates


class SessionCoverageError(ValueError):
    """La serie tiene sesiones ausentes o barras en días sin sesión."""

    def __init__(self, coverages: Sequence[SessionCoverage]) -> None:
        self.coverages = tuple(coverages)
        details = "; ".join(
            f"{coverage.symbol}: faltan {len(coverage.missing_sessions)} sesiones "
            f"{_preview(coverage.missing_sessions)}, "
            f"{len(coverage.non_session_dates)} barras fuera de sesión "
            f"{_preview(coverage.non_session_dates)}"
            for coverage in self.coverages
        )
        super().__init__(f"Series incompletas frente al calendario: {details}")


def check_session_coverage(
    symbol: str,
    bar_dates: Sequence[date],
    session_dates: Sequence[date],
) -> SessionCoverage:
    """Compara las fechas de las barras con las sesiones del calendario."""
    if not bar_dates:
        return SessionCoverage(
            symbol=symbol,
            first_date=None,
            last_date=None,
            expected_sessions=0,
            missing_sessions=(),
            non_session_dates=(),
        )

    first_date = min(bar_dates)
    last_date = max(bar_dates)
    present = set(bar_dates)
    sessions = set(session_dates)
    expected = sorted(
        session for session in sessions if first_date <= session <= last_date
    )

    return SessionCoverage(
        symbol=symbol,
        first_date=first_date,
        last_date=last_date,
        expected_sessions=len(expected),
        missing_sessions=tuple(
            session for session in expected if session not in present
        ),
        non_session_dates=tuple(sorted(present - sessions)),
    )


def _preview(dates: Sequence[date], limit: int = 5) -> str:
    if not dates:
        return "[]"

    shown = ", ".join(value.isoformat() for value in dates[:limit])
    suffix = ", …" if len(dates) > limit else ""
    return f"[{shown}{suffix}]"
