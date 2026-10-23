from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from algo_trading.backtesting import BacktestEngine, BacktestParameters, BacktestResult
from algo_trading.data.market_bar import MarketBar
from algo_trading.data.market_calendar import SessionCoverageError
from algo_trading.domain.corporate_actions.dto import CorporateDividend
from algo_trading.execution import ExitReason

START = datetime(2024, 1, 1, tzinfo=UTC)
INITIAL_CASH = Decimal("100000")
QUANTITY = 90  # entrada 110.055, limitada por exposición (ver test_backtest_engine)


def bar(index: int, ohlc: tuple[str, str, str, str]) -> MarketBar:
    open_price, high, low, close = (Decimal(value) for value in ohlc)
    return MarketBar(
        symbol="AAA",
        timestamp=START + timedelta(days=index),
        timeframe="1d",
        source="test",
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=Decimal("1000"),
    )


def day(index: int) -> date:
    return (START + timedelta(days=index)).date()


def time_exit_series() -> list[MarketBar]:
    """Señal en 20, entrada en 21, salida temporal en la apertura de 41."""
    bars = []

    for index in range(20):
        close = Decimal("100") + Decimal("0.1") * index
        bars.append(
            bar(index, (str(close), str(close + 1), str(close - 1), str(close)))
        )

    bars.append(bar(20, ("110", "111", "109", "110")))
    bars.extend(bar(index, ("110", "110.5", "109.5", "110")) for index in range(21, 43))
    return bars


def run(
    bars: list[MarketBar],
    trading_sessions: list[date] | None = None,
    dividends: list[CorporateDividend] | None = None,
) -> BacktestResult:
    return BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(
        {"AAA": bars},
        trading_sessions=trading_sessions,
        dividends_by_symbol={"AAA": dividends or []},
    )


def dividend(index: int, amount: str = "0.5") -> CorporateDividend:
    return CorporateDividend(
        symbol="AAA",
        ex_date=day(index),
        amount=Decimal(amount),
        source="test",
    )


# --- Calendario ----------------------------------------------------------


def test_complete_calendar_gives_same_result_as_bar_counting() -> None:
    bars = time_exit_series()
    sessions = [bar.timestamp.date() for bar in bars]

    with_calendar = run(bars, trading_sessions=sessions)
    without_calendar = run(bars)

    assert with_calendar.uses_trading_calendar
    assert not without_calendar.uses_trading_calendar
    assert with_calendar.trades == without_calendar.trades
    assert with_calendar.trades[0].exit_reason is ExitReason.TIME_EXIT


def test_missing_session_in_series_is_rejected() -> None:
    bars = time_exit_series()
    sessions = [bar.timestamp.date() for bar in bars]
    del bars[30]  # el calendario espera esa sesión

    with pytest.raises(SessionCoverageError, match="AAA: faltan 1 sesiones"):
        run(bars, trading_sessions=sessions)


def test_bar_on_non_session_date_is_rejected() -> None:
    bars = time_exit_series()
    sessions = [bar.timestamp.date() for bar in bars]
    sessions.remove(day(30))  # p. ej. un festivo con barra espuria

    with pytest.raises(SessionCoverageError, match="1 barras fuera de sesión"):
        run(bars, trading_sessions=sessions)


def test_time_exit_counts_calendar_sessions() -> None:
    bars = time_exit_series()
    sessions = [bar.timestamp.date() for bar in bars]

    trade = run(bars, trading_sessions=sessions).trades[0]

    assert trade.entry_timestamp.date() == day(21)
    assert trade.exit_timestamp.date() == day(41)
    assert trade.sessions_since_entry == 20


# --- Dividendos ----------------------------------------------------------


def test_open_position_receives_dividend_in_cash_on_ex_date() -> None:
    result = run(time_exit_series(), dividends=[dividend(25)])

    trade = result.trades[0]
    payment = result.dividends[0]
    assert payment.ex_date == day(25)
    assert payment.payment_timestamp.date() == day(25)
    assert payment.quantity == QUANTITY
    assert payment.total == Decimal("45.0")
    assert trade.dividend_income == Decimal("45.0")
    assert trade.net_pnl == trade.gross_pnl - trade.total_costs + Decimal("45.0")
    # El cash sube en la fecha ex-dividendo, no al cerrar la operación.
    assert result.equity_curve[25].cash - result.equity_curve[24].cash == Decimal(
        "45.0"
    )
    assert result.final_cash == INITIAL_CASH + trade.net_pnl


def test_dividend_on_entry_date_or_before_is_not_received() -> None:
    result = run(time_exit_series(), dividends=[dividend(20), dividend(21)])

    assert result.dividends == ()
    assert result.trades[0].dividend_income == 0


def test_dividend_after_exit_is_not_received() -> None:
    # La salida temporal es en la apertura de la sesión 41.
    result = run(time_exit_series(), dividends=[dividend(42)])

    assert result.dividends == ()


def test_dividend_on_exit_session_is_received() -> None:
    # Se poseía al cierre de la sesión 40: cobra aunque venda en la apertura de 41.
    result = run(time_exit_series(), dividends=[dividend(41)])

    assert [payment.ex_date for payment in result.dividends] == [day(41)]
    assert result.trades[0].dividend_income == Decimal("45.0")


def test_multiple_dividends_accumulate() -> None:
    result = run(
        time_exit_series(),
        dividends=[dividend(25, "0.5"), dividend(35, "0.25")],
    )

    assert result.trades[0].dividend_income == Decimal("67.50")
    assert len(result.dividends) == 2


def test_rejects_dividends_with_wrong_symbol() -> None:
    wrong = CorporateDividend(
        symbol="BBB",
        ex_date=day(25),
        amount=Decimal("1"),
        source="test",
    )

    with pytest.raises(ValueError, match="dividendos de AAA"):
        run(time_exit_series(), dividends=[wrong])


# --- Ventana de trading -------------------------------------------------------


def test_trading_start_blocks_entries_and_equity_before_it() -> None:
    bars = time_exit_series()

    result = BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(
        {"AAA": bars},
        trading_start=day(22),
    )

    # La señal de 20 entraría en 21, antes del inicio: no hay operación.
    assert result.trades == ()
    assert result.equity_curve[0].timestamp.date() == day(22)
    assert result.final_cash == INITIAL_CASH


def test_signal_before_trading_start_can_enter_on_first_session() -> None:
    bars = time_exit_series()

    windowed = BacktestEngine(BacktestParameters(initial_cash=INITIAL_CASH)).run(
        {"AAA": bars},
        trading_start=day(21),
    )
    full = run(bars)

    assert windowed.trades == full.trades
    assert windowed.trades[0].signal_timestamp.date() == day(20)
    assert len(windowed.equity_curve) == len(full.equity_curve) - 21
