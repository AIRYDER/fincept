from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from fincept_core.errors import ConfigError


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
    # Deployment environment: "dev" (local/test), "staging", or "production".
    # The runtime safety guard uses this to fail closed on the dev JWT secret
    # in non-dev envs (audit R4/P3).
    ENV: str = Field(default="dev")
    DB_URL: str = Field(default="")
    # Default uses 127.0.0.1 (not localhost) on purpose: on Windows,
    # 'localhost' resolves to ::1 (IPv6) first, but Memurai/Redis are
    # typically bound to IPv4 only.  The async resolver then blocks
    # waiting for an IPv6 connect that will never succeed.  Explicit
    # IPv4 dodges the tarpit.  On Linux/macOS dual-stack works either
    # way, so this default is safe everywhere.
    REDIS_URL: str = Field(default="redis://127.0.0.1:6379/0")
    OTEL_EXPORTER_OTLP_ENDPOINT: str = Field(default="http://127.0.0.1:4318")
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


# The dev-default JWT secret that must never reach a non-dev deployment.
_DEV_JWT_SECRET = "dev-only-change-me"
# Environments where the dev JWT secret is acceptable.
_DEV_ENVS = {"dev", "local", "test"}


def assert_safe_for_runtime(settings: Settings | None = None) -> None:
    """Fail closed if a non-dev environment is using the dev JWT secret.

    Every service entrypoint that touches Redis, streams, schedulers, or
    broker-adjacent clients must call this after ``get_settings()`` and
    before any side effect (audit R4 / P3).  In dev/local/test the dev
    default secret is allowed; in staging or production it raises
    ``ConfigError`` so the process refuses to start.

    Can be called with no arguments (uses the cached singleton) or with
    an explicit ``Settings`` instance (useful in tests that clear the
    cache and construct a fresh instance).
    """
    s = settings if settings is not None else get_settings()
    env = s.ENV.strip().lower()
    if env in _DEV_ENVS:
        return
    if s.JWT_SECRET == _DEV_JWT_SECRET or not s.JWT_SECRET.strip():
        raise ConfigError(
            f"FINCEPT_JWT_SECRET is the dev default (or empty) in "
            f"environment '{env}'. Set a strong secret before starting "
            f"any non-dev service. See audit R4/P3."
        )
