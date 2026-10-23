import hashlib
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import localcontext
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from algo_trading.analytics.report import BacktestReport, build_report, report_to_json
from algo_trading.backtesting.engine import BacktestEngine, BacktestParameters
from algo_trading.data.adjustments.split_adjustment import SplitAdjustmentCalculator
from algo_trading.data.market_bar import MarketBar
from algo_trading.data.market_calendar import TradingCalendar
from algo_trading.domain.corporate_actions.dto import CorporateDividend
from algo_trading.persistence.models.adjusted_market_data import MarketBarAdjusted
from algo_trading.persistence.repositories.adjusted_bar_query_repository import (
    AdjustedMarketBarQueryRepository,
)
from algo_trading.persistence.repositories.backtest_run_repository import (
    BacktestRunMetadata,
    BacktestRunRepository,
)
from algo_trading.persistence.repositories.corporate_action_query_repository import (
    CorporateActionQueryRepository,
)
from algo_trading.portfolio.position_sizing import MONEY_PRECISION, MONEY_ROUNDING
from algo_trading.signals import SignalContext, SignalGenerator


class MissingMarketDataError(ValueError):
    """Indica que algún símbolo no tiene barras ajustadas en el periodo."""


class BacktestRunRequest(BaseModel):
    """
    Qué datos ajustados leer de MySQL y con qué parámetros simular.

    `start` y `end` son fechas de sesión inclusivas. `reference_symbols` son
    series no operables (benchmarks, VIX, ETFs sectoriales) que se cargan
    para el `SignalContext` de los filtros v2; no entran en la cartera ni en
    el Buy & Hold.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = Field(min_length=1)
    start: date
    end: date
    parameters: BacktestParameters
    source: str = Field(default="yfinance", min_length=1, max_length=64)
    timeframe: str = Field(default="1d", min_length=1, max_length=16)
    adjustment_version: str = Field(default="splits-v1", min_length=1, max_length=64)
    include_dividends: bool = True
    reference_symbols: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.end < self.start:
            raise ValueError("end no puede ser anterior a start")

        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("symbols no puede contener duplicados")

        return self


class BacktestRunOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    report: BacktestReport
    run_id: str | None
    report_checksum: str


class PreparedMarketData(BaseModel):
    """Datos leídos una vez de MySQL y reutilizables entre ejecuciones."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bars_by_symbol: dict[str, tuple[MarketBar, ...]]
    dividends_by_symbol: dict[str, tuple[CorporateDividend, ...]]
    trading_sessions: tuple[date, ...] | None
    reference_bars: dict[str, tuple[MarketBar, ...]] = Field(default_factory=dict)


class BacktestRunService:
    """
    Ejecuta un backtest sobre los datos de MySQL y persiste su resultado.

    1. Lee `market_bars_adjusted` del periodo (fechas inclusivas) y las
       convierte a `MarketBar` con timestamp UTC.
    2. Lee los dividendos raw del periodo y los lleva a la escala de las
       barras ajustadas: `dividendo_ajustado = dividendo_raw * factor`, con el
       factor acumulado de los splits posteriores a la fecha ex-dividendo.
    3. Si hay calendario, valida que cada serie cubra todas sus sesiones y
       pasa las sesiones al motor (conteo exacto de sesiones).
    4. Ejecuta `BacktestEngine`, construye el `BacktestReport` y, si
       `persist`, guarda `BacktestRun`, `Trade` y `EquitySnapshot` en una
       transacción.
    """

    def __init__(
        self,
        session: Session,
        calendar: TradingCalendar | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._session = session
        self._calendar = calendar
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._bars = AdjustedMarketBarQueryRepository(session)
        self._corporate_actions = CorporateActionQueryRepository(session)
        self._runs = BacktestRunRepository(session)

    def load_bars(
        self, request: BacktestRunRequest
    ) -> dict[str, tuple[MarketBar, ...]]:
        start = datetime(request.start.year, request.start.month, request.start.day)
        end = datetime(request.end.year, request.end.month, request.end.day, 23, 59, 59)

        bars_by_symbol: dict[str, tuple[MarketBar, ...]] = {}
        missing: list[str] = []

        for symbol in request.symbols:
            rows = self._bars.get_by_symbol(
                symbol=symbol,
                start=start,
                end=end,
                timeframe=request.timeframe,
                source=request.source,
                adjustment_version=request.adjustment_version,
            )

            if not rows:
                missing.append(symbol)
                continue

            bars_by_symbol[symbol] = tuple(_to_market_bar(row) for row in rows)

        if missing:
            raise MissingMarketDataError(
                "Sin barras ajustadas en el periodo para: " + ", ".join(missing)
            )

        return bars_by_symbol

    def load_adjusted_dividends(
        self, request: BacktestRunRequest
    ) -> dict[str, tuple[CorporateDividend, ...]]:
        dividends_by_symbol: dict[str, tuple[CorporateDividend, ...]] = {}

        for symbol in request.symbols:
            splits = self._corporate_actions.get_split_dtos_by_symbol(
                symbol=symbol,
                source=request.source,
            )
            raw_dividends = self._corporate_actions.get_dividend_dtos_by_symbol(
                symbol=symbol,
                start=request.start,
                end=request.end,
                source=request.source,
            )

            with localcontext(prec=MONEY_PRECISION, rounding=MONEY_ROUNDING):
                dividends_by_symbol[symbol] = tuple(
                    dividend.model_copy(
                        update={
                            "amount": dividend.amount
                            * SplitAdjustmentCalculator.cumulative_factor_for_bar(
                                bar_date=dividend.ex_date,
                                splits=splits,
                            )
                        }
                    )
                    for dividend in raw_dividends
                )

        return dividends_by_symbol

    def prepare(self, request: BacktestRunRequest) -> PreparedMarketData:
        """Lee barras, dividendos, sesiones y series de referencia del periodo."""
        return PreparedMarketData(
            bars_by_symbol=self.load_bars(request),
            dividends_by_symbol=(
                self.load_adjusted_dividends(request)
                if request.include_dividends
                else {}
            ),
            trading_sessions=(
                tuple(
                    session.session_date
                    for session in self._calendar.sessions(request.start, request.end)
                )
                if self._calendar is not None
                else None
            ),
            reference_bars=(
                self.load_bars(
                    request.model_copy(update={"symbols": request.reference_symbols})
                )
                if request.reference_symbols
                else {}
            ),
        )

    def run(
        self,
        request: BacktestRunRequest,
        persist: bool = True,
        sweep_id: str | None = None,
        signal_generator: SignalGenerator | None = None,
    ) -> BacktestRunOutcome:
        return self.run_prepared(
            request,
            self.prepare(request),
            persist=persist,
            sweep_id=sweep_id,
            signal_generator=signal_generator,
        )

    def run_prepared(
        self,
        request: BacktestRunRequest,
        data: PreparedMarketData,
        persist: bool = True,
        sweep_id: str | None = None,
        signal_generator: SignalGenerator | None = None,
    ) -> BacktestRunOutcome:
        """
        Ejecuta el backtest con datos ya preparados.

        Permite variar `request.parameters` sin volver a leer MySQL (barridos).
        Con `signal_generator`, las señales salen de ese generador con las
        series de referencia preparadas como contexto; sin él, es la v1.
        """
        result = BacktestEngine(request.parameters).run(
            data.bars_by_symbol,
            trading_sessions=data.trading_sessions,
            dividends_by_symbol=data.dividends_by_symbol,
            signal_generator=signal_generator,
            signal_context=(
                SignalContext(data.reference_bars)
                if signal_generator is not None
                else None
            ),
        )
        report = build_report(
            result,
            data.bars_by_symbol,
            dividends_by_symbol=data.dividends_by_symbol,
            trading_calendar=self._calendar.name if self._calendar else None,
            signal_generator=signal_generator,
        )
        checksum = hashlib.sha256(report_to_json(report).encode("utf-8")).hexdigest()

        if not persist:
            return BacktestRunOutcome(
                report=report,
                run_id=None,
                report_checksum=checksum,
            )

        run_id = self._id_factory()

        try:
            self._runs.add(
                report,
                BacktestRunMetadata(
                    run_id=run_id,
                    created_at=self._clock(),
                    report_checksum=checksum,
                    source=request.source,
                    adjustment_version=request.adjustment_version,
                    start_date=request.start,
                    end_date=request.end,
                    sweep_id=sweep_id,
                ),
            )
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

        return BacktestRunOutcome(
            report=report, run_id=run_id, report_checksum=checksum
        )


def _to_market_bar(row: MarketBarAdjusted) -> MarketBar:
    return MarketBar(
        symbol=row.symbol,
        timestamp=row.timestamp.replace(tzinfo=UTC),
        timeframe=row.timeframe,
        source=row.source,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
    )


def default_report_name(symbols: Sequence[str], start: date, end: date) -> str:
    """Nombre determinista: `backtest_<símbolos>_<inicio>_<fin>`."""
    return f"backtest_{'-'.join(sorted(symbols))}_{start.isoformat()}_{end.isoformat()}"
