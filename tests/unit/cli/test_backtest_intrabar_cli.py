from datetime import date

import pytest
from pydantic import SecretStr

from algo_trading.cli.backtest import (
    build_intrabar_resolver,
    build_request,
    parse_args,
    report_label,
)
from algo_trading.config.settings import ApplicationSettings
from algo_trading.data.market_calendar import TradingSession
from algo_trading.engine import IntrabarResolver


class NoSessions:
    name = "TEST"

    def sessions(self, start: date, end: date) -> tuple[TradingSession, ...]:
        return ()


def settings(with_credentials: bool) -> ApplicationSettings:
    local = ApplicationSettings.from_yaml()
    return local.model_copy(
        update={
            "alpaca_api_key_id": "key" if with_credentials else None,
            "alpaca_api_secret_key": SecretStr("secret") if with_credentials else None,
        }
    )


def test_intrabar_resolution_is_off_by_default() -> None:
    arguments = parse_args([])

    assert not arguments.intrabar_resolution
    assert build_intrabar_resolver(arguments, settings(True), NoSessions()) is None


def test_requires_alpaca_credentials() -> None:
    arguments = parse_args(["--intrabar-resolution"])

    with pytest.raises(ValueError, match="ALPACA_API_KEY_ID"):
        build_intrabar_resolver(arguments, settings(False), NoSessions())


def test_requires_market_calendar() -> None:
    arguments = parse_args(["--intrabar-resolution", "--calendar", "none"])

    with pytest.raises(ValueError, match="calendario"):
        build_intrabar_resolver(arguments, settings(True), None)


def test_builds_resolver_and_tags_report() -> None:
    arguments = parse_args(
        ["--intrabar-resolution", "--symbols", "AAPL", "--end", "2024-12-31"]
    )
    local = settings(True)

    resolver = build_intrabar_resolver(arguments, local, NoSessions())

    assert isinstance(resolver, IntrabarResolver)
    assert report_label(
        "backtest", arguments, build_request(arguments, local), local
    ) == ("backtest_intrabar_AAPL_2020-01-01_2024-12-31")


@pytest.mark.parametrize("mode", ["--sweep", "--walk-forward"])
def test_only_available_for_single_backtests(mode: str) -> None:
    with pytest.raises(SystemExit):
        parse_args([mode, "--intrabar-resolution"])
