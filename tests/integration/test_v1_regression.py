"""
Regresión de la v1 (regla de continuidad de la v2).

Reejecuta, sin persistir, el backtest de confirmación de la v1 con los
valores por defecto bloqueados (`local.yaml`: universo diversified, 5
posiciones, 1 % de riesgo, SL 2 x ATR, TP 3 x ATR, exposición 20 %, perfil
base, calendario NYSE, dividendos en cash) sobre las barras ajustadas de
MySQL, y exige el mismo informe que la ejecución de referencia
`1fe790dc-6bf2-4c5c-aa41-35186937afc1`: mismo checksum y mismas métricas.

Se omite si MySQL no está disponible. Un split nuevo de algún símbolo del
universo reescribe su histórico ajustado y cambia legítimamente el checksum:
en ese caso hay que regenerar la referencia de forma explícita.
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from algo_trading.backtesting.run_service import BacktestRunService
from algo_trading.cli.backtest import build_calendar, build_request, parse_args
from algo_trading.config.settings import ApplicationSettings
from algo_trading.persistence.database import SessionFactory
from algo_trading.persistence.repositories.backtest_run_repository import (
    BacktestRunRepository,
)

REFERENCE_RUN_ID = "1fe790dc-6bf2-4c5c-aa41-35186937afc1"
REFERENCE_CHECKSUM = "1e500e90fcf885e9938cdaf66c9ee76a7f461a72166bc57b4f6ff23f506eed5c"
REFERENCE_START = date(2020, 1, 1)
REFERENCE_END = date(2026, 10, 22)
REFERENCE_SYMBOLS = (
    "AAPL",
    "CAT",
    "GLD",
    "GOOGL",
    "JNJ",
    "JPM",
    "MSFT",
    "PG",
    "QQQ",
    "SPY",
    "TLT",
    "XLE",
    "XLF",
    "XLV",
    "XOM",
)
# Métricas de la referencia a la escala de `backtest_runs` (8 y 10 decimales).
REFERENCE_FINAL_EQUITY = Decimal("126293.78701286")
REFERENCE_CUMULATIVE_RETURN = Decimal("0.2629378701")
REFERENCE_MAX_DRAWDOWN = Decimal("0.0813045639")
REFERENCE_PROFIT_FACTOR = Decimal("1.2112940245")
REFERENCE_WIN_RATE = Decimal("0.4759036145")
REFERENCE_DIVIDEND_INCOME = Decimal("5720.64300000")
REFERENCE_TOTAL_TRADES = 332


@pytest.fixture(scope="module")
def session() -> Iterator[Session]:
    with SessionFactory() as database_session:
        try:
            database_session.execute(text("SELECT 1"))
        except OperationalError as error:
            pytest.skip(f"MySQL no disponible: {error.orig}")

        yield database_session


def _quantize(value: Decimal | None, places: int) -> Decimal | None:
    return None if value is None else value.quantize(Decimal(1).scaleb(-places))


def test_reference_run_is_persisted(session: Session) -> None:
    summary = BacktestRunRepository(session).get_summary(REFERENCE_RUN_ID)

    assert summary is not None
    assert summary.report_checksum == REFERENCE_CHECKSUM
    assert summary.symbols == REFERENCE_SYMBOLS
    assert summary.start_date == REFERENCE_START
    assert summary.end_date == REFERENCE_END


def test_v1_defaults_reproduce_reference_run(session: Session) -> None:
    settings = ApplicationSettings.from_yaml()
    arguments = parse_args(
        [
            "--start",
            REFERENCE_START.isoformat(),
            "--end",
            REFERENCE_END.isoformat(),
            "--no-persist",
        ]
    )
    request = build_request(arguments, settings)

    risk = request.parameters.risk
    assert tuple(sorted(request.symbols)) == REFERENCE_SYMBOLS
    assert risk.max_positions == 5
    assert risk.risk_per_trade == Decimal("0.01")
    assert risk.stop_loss_atr_multiple == Decimal("2.0")
    assert risk.take_profit_atr_multiple == Decimal("3.0")
    assert risk.max_position_exposure == Decimal("0.20")
    assert request.parameters.cost_profile == "base"

    outcome = BacktestRunService(
        session, calendar=build_calendar(arguments.calendar)
    ).run(request, persist=False)
    metrics = outcome.report.metrics

    assert outcome.run_id is None
    assert _quantize(metrics.final_equity, 8) == REFERENCE_FINAL_EQUITY
    assert _quantize(metrics.cumulative_return, 10) == REFERENCE_CUMULATIVE_RETURN
    assert _quantize(metrics.max_drawdown.max_drawdown_pct, 10) == (
        REFERENCE_MAX_DRAWDOWN
    )
    assert _quantize(metrics.trades.profit_factor, 10) == REFERENCE_PROFIT_FACTOR
    assert _quantize(metrics.trades.win_rate, 10) == REFERENCE_WIN_RATE
    assert _quantize(metrics.dividend_income, 8) == REFERENCE_DIVIDEND_INCOME
    assert metrics.trades.total_trades == REFERENCE_TOTAL_TRADES
    assert outcome.report_checksum == REFERENCE_CHECKSUM
