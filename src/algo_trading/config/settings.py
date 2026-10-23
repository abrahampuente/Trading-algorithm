from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, SecretStr, model_validator
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


DEFAULT_UNIVERSE = "diversified"


class StrategySettings(BaseModel):
    """
    Parámetros por defecto de la estrategia v1 bloqueados tras el walk-forward.

    Las CLIs los usan cuando no se indican explícitamente. Los valores por
    defecto de `RiskParameters` (especificación original) no cambian.
    """

    risk_per_trade: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)
    stop_loss_atr_multiple: Decimal = Field(default=Decimal("2.0"), gt=0)
    take_profit_atr_multiple: Decimal = Field(default=Decimal("3.0"), gt=0)
    max_position_exposure: Decimal = Field(default=Decimal("0.20"), gt=0, le=1)


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
        "macro_benchmarks": (
            "SPY",
            "QQQ",
            "^VIX",
            "XLK",
            "XLF",
            "XLV",
            "XLE",
            "XLY",
            "XLP",
            "XLI",
            "XLC",
            "XLU",
            "XLB",
            "XLRE",
        ),
    }


class SignalSettings(BaseModel):
    """
    Configuración de los filtros de régimen de la v2 (`signals/`).

    Sólo se usa cuando la CLI activa un filtro; sin filtros, el backtest es el
    de la v1. `reference_universe` son las series de referencia que se cargan
    en el `SignalContext` (benchmarks, VIX y ETFs sectoriales).
    """

    reference_universe: str = "macro_benchmarks"
    benchmark: str = "SPY"
    vix_symbol: str = "^VIX"
    relative_strength_lookback: int = Field(default=63, ge=1)
    sector_top_k: int = Field(default=4, ge=1)
    sector_allow_unmapped: bool = True
    sector_etfs: dict[str, str] = Field(default_factory=dict)
    sector_map: dict[str, str] = Field(default_factory=dict)


class IntradaySettings(BaseModel):
    """
    Proveedor intradía (Hito 2): Alpaca Market Data v2.

    Las credenciales no van aquí: `ALPACA_API_KEY_ID` y
    `ALPACA_API_SECRET_KEY` se leen del entorno o de `.env`.
    """

    provider: Literal["alpaca"] = "alpaca"
    feed: Literal["iex", "sip"] = "iex"
    data_url: str = "https://data.alpaca.markets"
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=5, ge=1)
    base_delay_seconds: float = Field(default=1.0, ge=0)
    max_delay_seconds: float = Field(default=60.0, ge=0)
    price_tolerance: Decimal = Field(default=Decimal("0.01"), ge=0)


class BrokerSettings(BaseModel):
    """
    Broker de paper trading (Hito 3). La v2 sólo admite Alpaca Paper: la URL
    no es configurable a otra cosa y `ALPACA_PAPER=true` es obligatorio en el
    entorno (`PaperTradingGuard`).
    """

    name: Literal["alpaca"] = "alpaca"
    environment: Literal["paper"] = "paper"
    base_url: Literal["https://paper-api.alpaca.markets"] = (
        "https://paper-api.alpaca.markets"
    )
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=3, ge=1)
    gap_threshold_atr: Decimal = Field(default=Decimal("0.5"), ge=0)


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
    default_universe: str = DEFAULT_UNIVERSE
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)
    intraday: IntradaySettings = Field(default_factory=IntradaySettings)
    broker: BrokerSettings = Field(default_factory=BrokerSettings)

    @model_validator(mode="after")
    def validate_default_universe(self) -> "ApplicationSettings":
        if self.default_universe.lower() not in {
            name.lower() for name in self.universes
        }:
            raise ValueError(
                f"default_universe '{self.default_universe}' no está en universes"
            )

        return self

    database_password: str | None = None
    alpaca_api_key_id: str | None = None
    # Acepta también ALPACA_SECRET_KEY, el nombre habitual en la documentación
    # de Alpaca.
    alpaca_api_secret_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("alpaca_api_secret_key", "alpaca_secret_key"),
    )
    # Texto, no bool: PaperTradingGuard sólo acepta el valor exacto "true".
    alpaca_paper: str | None = None

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
