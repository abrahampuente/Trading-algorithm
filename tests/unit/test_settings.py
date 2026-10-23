from decimal import Decimal

import pytest

from algo_trading.config.settings import (
    ApplicationSettings,
    default_cost_profiles,
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
