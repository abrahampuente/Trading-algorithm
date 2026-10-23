from decimal import Decimal

import pytest

from algo_trading.config.settings import (
    ApplicationSettings,
    default_cost_profiles,
    default_universes,
)


def test_load_local_settings() -> None:
    settings = ApplicationSettings.from_yaml()

    assert settings.environment == "local"
    assert settings.app_name == "algo-trading"
    assert settings.initial_capital == Decimal("100000.00")
    assert settings.max_positions == 5
    assert settings.database.host == "localhost"
    assert settings.database.port == 3306


def test_local_settings_define_three_cost_profiles() -> None:
    settings = ApplicationSettings.from_yaml()

    assert sorted(settings.cost_profiles) == ["base", "conservador", "optimista"]
    assert settings.cost_profile("optimista").commission_rate == Decimal("0.0002")
    assert settings.cost_profile("optimista").slippage_rate == Decimal("0.0002")
    assert settings.cost_profile("BASE").commission_rate == Decimal("0.0005")
    assert settings.cost_profile("conservador").slippage_rate == Decimal("0.0010")


def test_unknown_cost_profile_lists_available_profiles() -> None:
    settings = ApplicationSettings.from_yaml()

    with pytest.raises(ValueError, match="base, conservador, optimista"):
        settings.cost_profile("agresivo")


def test_default_cost_profiles_match_local_configuration() -> None:
    assert default_cost_profiles() == ApplicationSettings.from_yaml().cost_profiles


def test_local_settings_define_initial_and_diversified_universes() -> None:
    settings = ApplicationSettings.from_yaml()

    assert settings.universe("initial") == ("AAPL", "MSFT", "GOOGL", "SPY", "QQQ")
    assert settings.universe("diversified") == (
        "AAPL",
        "MSFT",
        "GOOGL",
        "QQQ",
        "SPY",
        "XOM",
        "JPM",
        "JNJ",
        "PG",
        "CAT",
        "GLD",
        "TLT",
        "XLE",
        "XLF",
        "XLV",
    )


def test_local_settings_lock_v1_strategy_defaults() -> None:
    settings = ApplicationSettings.from_yaml()

    assert settings.max_positions == 5
    assert settings.default_universe == "diversified"
    assert settings.strategy.risk_per_trade == Decimal("0.01")
    assert settings.strategy.stop_loss_atr_multiple == Decimal("2.0")
    assert settings.strategy.take_profit_atr_multiple == Decimal("3.0")
    assert settings.strategy.max_position_exposure == Decimal("0.20")


def test_default_universe_must_exist() -> None:
    settings = ApplicationSettings.from_yaml()

    with pytest.raises(ValueError, match="default_universe"):
        ApplicationSettings.model_validate(
            {**settings.model_dump(), "default_universe": "global"}
        )


def test_local_settings_define_macro_benchmarks_and_signal_filters() -> None:
    settings = ApplicationSettings.from_yaml()
    signals = settings.signals

    assert (
        settings.universe("macro_benchmarks") == default_universes()["macro_benchmarks"]
    )
    assert "^VIX" in settings.universe(signals.reference_universe)
    assert signals.benchmark == "SPY"
    assert signals.vix_symbol == "^VIX"
    assert len(signals.sector_etfs) == 11
    assert set(signals.sector_etfs.values()) <= set(
        settings.universe(signals.reference_universe)
    )
    assert set(signals.sector_map.values()) <= set(signals.sector_etfs)
    assert set(signals.sector_map) <= set(settings.universe("diversified"))
