"""
Ingesta de datos diarios reales desde Yahoo Finance hacia MySQL.

Uso:

    python -m algo_trading.cli.ingest
    python -m algo_trading.cli.ingest --symbols AAPL MSFT --start 2020-01-01
    python -m algo_trading.cli.ingest --universe diversified

Para cada símbolo:

1. Reanuda desde el día siguiente a la última barra raw almacenada (o desde
   `--start` si no hay datos).
2. Guarda un snapshot con `MarketDataSnapshotIngestionService` (barras raw,
   splits y dividendos, con checksum y trazabilidad).
3. Regenera sus barras ajustadas con `AdjustedMarketDataService`.

Un fallo en un símbolo no detiene al resto; el código de salida es 1 si
alguno falla.
"""

import argparse
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from algo_trading.data.adjustments.adjusted_market_data_service import (
    AdjustedMarketDataService,
)
from algo_trading.data.market_data_snapshot_ingestion_service import (
    MarketDataSnapshotIngestionService,
)
from algo_trading.data.providers.dto import (
    CorporateActionRequest,
    DataRequest,
    Timeframe,
)
from algo_trading.data.providers.yfinance import YFINANCE_SOURCE, YFinanceProvider
from algo_trading.persistence.repositories.corporate_action_query_repository import (
    CorporateActionQueryRepository,
)
from algo_trading.persistence.repositories.market_bar_query_repository import (
    RawMarketBarQueryRepository,
)

if TYPE_CHECKING:
    from algo_trading.config.settings import ApplicationSettings

DEFAULT_SYMBOLS = ("AAPL", "MSFT", "GOOGL", "SPY", "QQQ")
DEFAULT_UNIVERSE = "initial"
DEFAULT_START = date(2020, 1, 1)
DEFAULT_ADJUSTMENT_VERSION = "splits-v1"
DAILY = Timeframe.DAILY.value


class IngestionStatus(StrEnum):
    INGESTED = "INGESTED"
    UP_TO_DATE = "UP_TO_DATE"
    FAILED = "FAILED"


class SymbolIngestionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    status: IngestionStatus
    window_start: date | None = None
    window_end: date | None = None
    snapshot_id: str | None = None
    bars: int = 0
    splits: int = 0
    dividends: int = 0
    adjusted_bars: int = 0
    error: str | None = None


def default_end_date() -> date:
    """Ayer en UTC: evita guardar la barra incompleta de la sesión en curso."""
    return datetime.now(UTC).date() - timedelta(days=1)


def ingest_symbol(
    symbol: str,
    start: date,
    end: date,
    provider: YFinanceProvider,
    session: Session,
    adjustment_version: str = DEFAULT_ADJUSTMENT_VERSION,
) -> SymbolIngestionResult:
    raw_query = RawMarketBarQueryRepository(session)
    latest = raw_query.get_latest_timestamp(symbol, DAILY, YFINANCE_SOURCE)
    window_start = (
        max(start, latest.date() + timedelta(days=1)) if latest is not None else start
    )

    result = SymbolIngestionResult(
        symbol=symbol,
        status=IngestionStatus.UP_TO_DATE,
        window_start=window_start,
        window_end=end,
    )

    if window_start <= end:
        data_request = DataRequest(
            symbols=(symbol,),
            start=datetime(
                window_start.year, window_start.month, window_start.day, tzinfo=UTC
            ),
            end=datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC),
            timeframe=DAILY,
            source=YFINANCE_SOURCE,
        )
        corporate_action_request = CorporateActionRequest(
            symbols=(symbol,),
            start=window_start,
            end=end,
            source=YFINANCE_SOURCE,
        )

        # La descarga queda en la caché del proveedor; si no hay barras nuevas
        # (fin de semana, festivo) no se crea un snapshot vacío.
        if provider.get_bars(data_request):
            metadata = MarketDataSnapshotIngestionService(provider, session).ingest(
                data_request=data_request,
                corporate_action_request=corporate_action_request,
            )
            result = result.model_copy(
                update={
                    "status": IngestionStatus.INGESTED,
                    "snapshot_id": metadata.snapshot_id,
                    "bars": metadata.bars_count,
                    "splits": metadata.splits_count,
                    "dividends": metadata.dividends_count,
                }
            )

    raw_bars = raw_query.get_all_by_symbol(symbol, DAILY, YFINANCE_SOURCE)
    splits = CorporateActionQueryRepository(session).get_split_dtos_by_symbol(
        symbol=symbol,
        source=YFINANCE_SOURCE,
    )
    adjusted_bars = AdjustedMarketDataService(
        session, adjustment_version
    ).rebuild_for_symbol(
        symbol=symbol,
        source=YFINANCE_SOURCE,
        timeframe=DAILY,
        raw_bars=raw_bars,
        splits=splits,
    )

    return result.model_copy(update={"adjusted_bars": adjusted_bars})


def run_ingestion(
    symbols: Sequence[str],
    start: date,
    end: date,
    provider: YFinanceProvider,
    session_factory: Callable[[], Session],
    adjustment_version: str = DEFAULT_ADJUSTMENT_VERSION,
) -> list[SymbolIngestionResult]:
    if end < start:
        raise ValueError("end no puede ser anterior a start")

    results: list[SymbolIngestionResult] = []

    for symbol in symbols:
        with session_factory() as session:
            try:
                results.append(
                    ingest_symbol(
                        symbol=symbol,
                        start=start,
                        end=end,
                        provider=provider,
                        session=session,
                        adjustment_version=adjustment_version,
                    )
                )
            except Exception as error:
                session.rollback()
                results.append(
                    SymbolIngestionResult(
                        symbol=symbol,
                        status=IngestionStatus.FAILED,
                        error=f"{type(error).__name__}: {error}",
                    )
                )

    return results


def format_results(
    results: Sequence[SymbolIngestionResult],
    provider: YFinanceProvider,
) -> str:
    lines = [
        f"{'Símbolo':<8} {'Estado':<11} {'Ventana':<23} {'Barras':>7} "
        f"{'Splits':>6} {'Divid.':>6} {'Ajust.':>7}",
    ]

    for result in results:
        has_window = (
            result.window_start is not None
            and result.window_end is not None
            and result.window_start <= result.window_end
        )
        window = f"{result.window_start} → {result.window_end}" if has_window else "-"
        lines.append(
            f"{result.symbol:<8} {result.status:<11} {window:<23} "
            f"{result.bars:>7} {result.splits:>6} {result.dividends:>6} "
            f"{result.adjusted_bars:>7}"
        )

        if result.error:
            lines.append(f"    error: {result.error}")

    for rejected in provider.rejected_rows:
        lines.append(
            f"Fila descartada {rejected.symbol} {rejected.session_date}: "
            f"{rejected.reason}"
        )

    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="algo-ingest",
        description="Descarga datos diarios de Yahoo Finance y puebla MySQL.",
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Símbolos a ingerir.",
    )
    selection.add_argument(
        "--universe",
        default=None,
        help="Universo de la configuración (por defecto: initial).",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=DEFAULT_START,
        help="Fecha inicial YYYY-MM-DD (por defecto: %(default)s).",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        help="Fecha final inclusiva YYYY-MM-DD (por defecto: ayer en UTC).",
    )
    parser.add_argument(
        "--adjustment-version",
        default=DEFAULT_ADJUSTMENT_VERSION,
        help="Versión de ajuste de market_bars_adjusted (por defecto: %(default)s).",
    )
    return parser.parse_args(argv)


def resolve_symbols(
    arguments: argparse.Namespace,
    settings: "ApplicationSettings",
) -> tuple[str, ...]:
    """`--symbols` explícitos, o los del `--universe` (por defecto `initial`)."""
    if arguments.symbols:
        return tuple(symbol.upper() for symbol in arguments.symbols)

    return settings.universe(arguments.universe or DEFAULT_UNIVERSE)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)

    from algo_trading.config.settings import ApplicationSettings

    symbols = list(resolve_symbols(arguments, ApplicationSettings.from_yaml()))
    end: date = arguments.end or default_end_date()
    provider = YFinanceProvider()

    from algo_trading.persistence.database import SessionFactory

    results = run_ingestion(
        symbols=symbols,
        start=arguments.start,
        end=end,
        provider=provider,
        session_factory=SessionFactory,
        adjustment_version=arguments.adjustment_version,
    )
    print(format_results(results, provider))

    return 1 if any(r.status is IngestionStatus.FAILED for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
