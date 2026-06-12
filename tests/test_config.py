import pytest

from autoreply.config import Settings


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost")
    monkeypatch.setenv("OWNER_ID", "456")
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)

    settings = Settings.from_env()

    assert settings.api_id == 123
    assert settings.mongodb_database == "telegram_autoreply"
    assert settings.owner_id == 456


def test_settings_reports_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_BOT_TOKEN",
        "MONGODB_URI",
        "OWNER_ID",
    ]:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="Missing required environment variables"):
        Settings.from_env()
