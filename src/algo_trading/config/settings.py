from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCAL_CONFIG_PATH = PROJECT_ROOT / "configs" / "environments" / "local.yaml"


class CostProfileSettings(BaseModel):
    """Costes por ejecución expresados como fracción (0.0005 = 0,05 %)."""

    commission_rate: Decimal = Field(ge=0, lt=1)
    slippage_rate: Decimal = Field(ge=0, lt=1)


DEFAULT_COST_PROFILE = "base"


def default_cost_profiles() -> dict[str, CostProfileSettings]:
    """Perfiles de la v1: optimista, base y conservador."""
    return {
        "optimista": CostProfileSettings(
            commission_rate=Decimal("0.0002"),
            slippage_rate=Decimal("0.0002"),
        ),
        "base": CostProfileSettings(
            commission_rate=Decimal("0.0005"),
            slippage_rate=Decimal("0.0005"),
        ),
        "conservador": CostProfileSettings(
            commission_rate=Decimal("0.0010"),
            slippage_rate=Decimal("0.0010"),
        ),
    }


DEFAULT_UNIVERSE = "initial"


def default_universes() -> dict[str, tuple[str, ...]]:
    """Universos de símbolos reutilizables por las CLIs."""
    return {
        "initial": ("AAPL", "MSFT", "GOOGL", "SPY", "QQQ"),
        "diversified": (
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
        ),
    }


class DatabaseSettings(BaseModel):
    host: str
    port: int = Field(gt=0, le=65535)
    name: str
    user: str
    password: str | None = None


class ApplicationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Literal["local", "testing", "production"]
    app_name: str
    log_level: str = "INFO"

    initial_capital: Decimal = Field(gt=0)
    max_positions: int = Field(gt=0)

    database: DatabaseSettings

    cost_profiles: dict[str, CostProfileSettings] = Field(
        default_factory=default_cost_profiles,
        min_length=1,
    )
    universes: dict[str, tuple[str, ...]] = Field(
        default_factory=default_universes,
        min_length=1,
    )

    database_password: str | None = None

    @classmethod
    def from_yaml(
        cls,
        config_path: Path = LOCAL_CONFIG_PATH,
    ) -> "ApplicationSettings":
        with config_path.open("r", encoding="utf-8") as config_file:
            yaml_values = yaml.safe_load(config_file) or {}

        database_values = yaml_values.get("database", {})
        yaml_values["database"] = {
            **database_values,
            "password": None,
        }

        settings = cls(**yaml_values)

        if settings.database_password:
            settings.database.password = settings.database_password

        return settings

    def universe(self, name: str) -> tuple[str, ...]:
        """Símbolos de un universo (nombre sin distinguir mayúsculas)."""
        universes = {key.lower(): value for key, value in self.universes.items()}

        try:
            symbols = universes[name.lower()]
        except KeyError:
            available = ", ".join(sorted(self.universes))
            raise ValueError(
                f"Universo desconocido: {name}. Disponibles: {available}"
            ) from None

        return tuple(symbol.upper() for symbol in symbols)

    def cost_profile(self, name: str) -> CostProfileSettings:
        """Devuelve un perfil de costes por nombre (sin distinguir mayúsculas)."""
        profiles = {key.lower(): value for key, value in self.cost_profiles.items()}

        try:
            return profiles[name.lower()]
        except KeyError:
            available = ", ".join(sorted(self.cost_profiles))
            raise ValueError(
                f"Perfil de costes desconocido: {name}. Disponibles: {available}"
            ) from None
