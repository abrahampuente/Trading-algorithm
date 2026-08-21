from decimal import Decimal

from algo_trading.config.settings import ApplicationSettings


def test_load_local_settings() -> None:
    settings = ApplicationSettings.from_yaml()

    assert settings.environment == "local"
    assert settings.app_name == "algo-trading"
    assert settings.initial_capital == Decimal("100000.00")
    assert settings.max_positions == 5
    assert settings.database.host == "localhost"
    assert settings.database.port == 3306
