from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FINCEPT_", extra="ignore")

    TRADING_MODE: str = "paper"
    DB_URL: str = ""
    REDIS_URL: str = "redis://localhost:6379/0"
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4318"


@lru_cache(maxsize=1)
def SettingsSingleton() -> Settings:
    return Settings()
