from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCAL_CONFIG_PATH = PROJECT_ROOT / "configs" / "environments" / "local.yaml"


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
