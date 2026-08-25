from sqlalchemy import Engine

from algo_trading.config.settings import ApplicationSettings
from algo_trading.persistence.database import (
    build_database_url,
    create_database_engine,
    engine,
)
from algo_trading.persistence.models import Base


def test_database_engine_is_configured() -> None:
    assert isinstance(engine, Engine)
    assert engine.url.drivername == "mysql+pymysql"


def test_build_database_url() -> None:
    settings = ApplicationSettings.from_yaml()

    database_url = build_database_url(settings)

    assert database_url == (
        "mysql+pymysql://algo_trading_app:"
        "algo-trading-dev-password@localhost:3306/algo_trading"
    )


def test_create_database_engine() -> None:
    settings = ApplicationSettings.from_yaml()

    database_engine = create_database_engine(settings)

    assert isinstance(database_engine, Engine)
    assert database_engine.url.database == "algo_trading"


def test_declarative_base_has_metadata() -> None:
    assert Base.metadata is not None
