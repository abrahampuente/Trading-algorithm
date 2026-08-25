from collections.abc import Generator
from urllib.parse import quote_plus

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from algo_trading.config.settings import ApplicationSettings


def build_database_url(settings: ApplicationSettings) -> str:
    """Construye la URL de conexión a MySQL desde la configuración."""
    database = settings.database

    user = quote_plus(database.user)
    password = quote_plus(database.password or "")

    return (
        f"mysql+pymysql://{user}:{password}@"
        f"{database.host}:{database.port}/{database.name}"
    )


def create_database_engine(settings: ApplicationSettings) -> Engine:
    """Crea un engine SQLAlchemy sin abrir todavía una conexión."""
    return create_engine(
        build_database_url(settings),
        pool_pre_ping=True,
        echo=False,
    )


_settings = ApplicationSettings.from_yaml()
engine = create_database_engine(_settings)

SessionFactory = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_session() -> Generator[Session]:
    """Proporciona una sesión y garantiza su cierre."""
    session = SessionFactory()

    try:
        yield session
    finally:
        session.close()
