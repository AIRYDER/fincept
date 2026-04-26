from fincept_core.config import Settings


def test_settings_load_from_env_and_singleton(monkeypatch):
    Settings.clear_cache()
    monkeypatch.setenv("FINCEPT_DB_URL", "postgresql+psycopg://user:pass@localhost/db")
    monkeypatch.setenv("FINCEPT_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("FINCEPT_TRADING_MODE", "paper")
    first = Settings()
    second = Settings()
    assert first is second
    assert first.DB_URL == "postgresql+psycopg://user:pass@localhost/db"
    assert first.REDIS_URL == "redis://localhost:6379/0"
    assert first.TRADING_MODE == "paper"
