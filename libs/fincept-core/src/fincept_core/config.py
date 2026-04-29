from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINCEPT_", extra="ignore")

    TRADING_MODE: str = Field(default="paper")
    DB_URL: str = Field(default="")
    REDIS_URL: str = Field(default="redis://localhost:6379/0")
    OTEL_EXPORTER_OTLP_ENDPOINT: str = Field(default="http://localhost:4318")
    LOG_LEVEL: str = Field(default="INFO")
    BINANCE_API_KEY: str | None = Field(default=None)
    BINANCE_API_SECRET: str | None = Field(default=None)
    OPENAI_API_KEY: str | None = Field(default=None)
    ANTHROPIC_API_KEY: str | None = Field(default=None)
    POLYGON_API_KEY: str | None = Field(default=None)
    ALPACA_API_KEY: str | None = Field(default=None)
    ALPACA_API_SECRET: str | None = Field(default=None)
    ALPACA_BASE_URL: str = Field(default="https://paper-api.alpaca.markets")
    UNIVERSE: list[str] = Field(default_factory=lambda: ["BTC-USD", "ETH-USD", "SOL-USD"])
    DEFAULT_VENUE: str = Field(default="binance")
    MAX_NOTIONAL_USD_PER_SYMBOL: int = Field(default=10000)
    MAX_GROSS_NOTIONAL_USD: int = Field(default=50000)
    MAX_DAILY_LOSS_USD: int = Field(default=2000)

    def __new__(cls, *args: Any, **kwargs: Any) -> Settings:
        global _SETTINGS_INSTANCE
        if _SETTINGS_INSTANCE is None or not isinstance(_SETTINGS_INSTANCE, cls):
            _SETTINGS_INSTANCE = super().__new__(cls)
        return _SETTINGS_INSTANCE

    @classmethod
    def clear_cache(cls) -> None:
        global _SETTINGS_INSTANCE
        _SETTINGS_INSTANCE = None


_SETTINGS_INSTANCE: Settings | None = None


def get_settings() -> Settings:
    return Settings()
