from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    # ``env_file`` is searched relative to the process CWD by
    # pydantic-settings.  All our entrypoints (uvicorn, scripts/, agents)
    # are invoked from the repo root, so a single .env at the root is
    # picked up by every service.  Shell environment variables still
    # override the .env file (standard pydantic-settings precedence).
    model_config = SettingsConfigDict(
        env_prefix="FINCEPT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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
    # Auxiliary data providers - optional.  Each consumer must handle
    # the None case (skip the provider, fall back to alternates) so a
    # missing key is a feature gate, not a crash.
    FRED_API_KEY: str | None = Field(default=None)
    NEWSAPI_API_KEY: str | None = Field(default=None)
    FINNHUB_API_KEY: str | None = Field(default=None)
    TIINGO_API_KEY: str | None = Field(default=None)
    # Tinker = Thinking Machines Lab (research-tier LLM tokens with
    # the `tml-` prefix).  Currently unused; reserved for future
    # research agents that prefer TML over OpenAI/Anthropic.
    TINKER_API_KEY: str | None = Field(default=None)
    # Which LLM provider the sentiment agent (and any future LLM-based
    # agent) should prefer.  "auto" picks the first configured key in
    # order (Anthropic, OpenAI).  Override to "openai" to force OpenAI
    # when your Anthropic account is out of credits, or vice versa.
    LLM_PROVIDER: str = Field(default="auto")
    # API auth secret (HS256 JWT signing).  The dev default is intentionally
    # unsafe so production deploys must set FINCEPT_JWT_SECRET explicitly.
    JWT_SECRET: str = Field(default="dev-only-change-me")
    UNIVERSE: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["BTC-USD", "ETH-USD", "SOL-USD"]
    )
    DEFAULT_VENUE: str = Field(default="binance")
    # OMS routing: "sim" sends OrderIntents through the in-process PaperFiller;
    # "alpaca" submits to Alpaca via REST (paper or live based on ALPACA_BASE_URL).
    # Explicit setting prevents silent route changes when operators provision
    # ALPACA_API_KEY for other reasons (e.g., the future equity data loader).
    OMS_ROUTER: str = Field(default="sim")
    MAX_NOTIONAL_USD_PER_SYMBOL: int = Field(default=10000)
    MAX_GROSS_NOTIONAL_USD: int = Field(default=50000)
    MAX_DAILY_LOSS_USD: int = Field(default=2000)

    @field_validator("UNIVERSE", mode="before")
    @classmethod
    def _parse_universe(cls, value: Any) -> Any:
        """Accept JSON array, comma-separated string, or real list.

        pydantic-settings normally assumes list[str] env vars are JSON;
        operators naturally write ``FINCEPT_UNIVERSE=BTC-USD,ETH-USD``
        though, so this coerces that shape before validation.
        """
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    pass
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value

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
