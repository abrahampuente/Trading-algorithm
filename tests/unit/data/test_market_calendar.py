from datetime import UTC, date, datetime

import pytest

from algo_trading.data.market_calendar import (
    ExchangeCalendar,
    SessionCoverageError,
    check_session_coverage,
)


@pytest.fixture(scope="module")
def nyse() -> ExchangeCalendar:
    return ExchangeCalendar("NYSE")


def test_nyse_excludes_weekends_and_holidays(nyse: ExchangeCalendar) -> None:
    sessions = nyse.sessions(date(2024, 11, 26), date(2024, 12, 2))

    assert [session.session_date for session in sessions] == [
        date(2024, 11, 26),
        date(2024, 11, 27),
        # 2024-11-28 Thanksgiving; 30-11 y 01-12 fin de semana.
        date(2024, 11, 29),
        date(2024, 12, 2),
    ]


def test_nyse_flags_early_closes(nyse: ExchangeCalendar) -> None:
    sessions = {
        session.session_date: session
        for session in nyse.sessions(date(2024, 11, 27), date(2024, 12, 24))
    }

    black_friday = sessions[date(2024, 11, 29)]
    assert black_friday.is_early_close
    assert black_friday.market_close == datetime(2024, 11, 29, 18, 0, tzinfo=UTC)
    assert sessions[date(2024, 12, 24)].is_early_close
    assert not sessions[date(2024, 11, 27)].is_early_close
    assert sessions[date(2024, 11, 27)].market_close == datetime(
        2024, 11, 27, 21, 0, tzinfo=UTC
    )


def test_nyse_includes_special_closures(nyse: ExchangeCalendar) -> None:
    dates = {
        session.session_date
        for session in nyse.sessions(date(2025, 1, 8), date(2025, 1, 10))
    }

    # 2025-01-09: cierre por el funeral de Estado del expresidente Carter.
    assert dates == {date(2025, 1, 8), date(2025, 1, 10)}


def test_nyse_2024_has_252_sessions(nyse: ExchangeCalendar) -> None:
    assert len(nyse.sessions(date(2024, 1, 1), date(2024, 12, 31))) == 252
    assert nyse.name == "NYSE"


def test_sessions_reject_inverted_range(nyse: ExchangeCalendar) -> None:
    with pytest.raises(ValueError, match="end"):
        nyse.sessions(date(2024, 2, 1), date(2024, 1, 1))


SESSIONS = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4), date(2024, 1, 5))


def test_complete_coverage() -> None:
    coverage = check_session_coverage("AAA", list(SESSIONS[1:3]), SESSIONS)

    assert coverage.is_complete
    assert coverage.expected_sessions == 2


def test_detects_missing_sessions_within_series_range() -> None:
    coverage = check_session_coverage(
        "AAA",
        [date(2024, 1, 2), date(2024, 1, 5)],
        SESSIONS,
    )

    assert not coverage.is_complete
    assert coverage.missing_sessions == (date(2024, 1, 3), date(2024, 1, 4))
    assert coverage.expected_sessions == 4


def test_detects_bars_on_non_session_dates() -> None:
    coverage = check_session_coverage(
        "AAA",
        [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 6)],
        SESSIONS,
    )

    assert coverage.non_session_dates == (date(2024, 1, 6),)
    # El 4 y el 5 caen dentro del rango de la serie y faltan.
    assert coverage.missing_sessions == (date(2024, 1, 4), date(2024, 1, 5))


def test_coverage_error_lists_symbols_and_dates() -> None:
    coverage = check_session_coverage(
        "AAA", [date(2024, 1, 2), date(2024, 1, 4)], SESSIONS
    )

    error = SessionCoverageError([coverage])

    assert "AAA" in str(error)
    assert "2024-01-03" in str(error)
    assert error.coverages == (coverage,)
