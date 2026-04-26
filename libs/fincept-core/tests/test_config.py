from fincept_core.config import Settings


def test_settings_load_from_env(monkeypatch):
    monkeypatch.setenv("FINCEPT_DB_URL", "postgresql+psycopg://user:pass@localhost/db")
    monkeypatch.setenv("FINCEPT_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("FINCEPT_TRADING_MODE", "paper")
    settings = Settings()
    assert settings.DB_URL == "postgresql+psycopg://user:pass@localhost/db"
    assert settings.REDIS_URL == "redis://localhost:6379/0"
    assert settings.TRADING_MODE == "paper"
