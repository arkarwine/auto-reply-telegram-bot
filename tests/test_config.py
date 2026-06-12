import pytest

from autoreply.config import Settings


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost")
    monkeypatch.setenv("UPDATES", "https://t.me/updates")
    monkeypatch.setenv("SUPPORT", "https://t.me/support")
    monkeypatch.setenv("OWNER_LINK", "https://t.me/owner")
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)

    settings = Settings.from_env()

    assert settings.api_id == 123
    assert settings.mongodb_database == "telegram_autoreply"
    assert settings.updates == "https://t.me/updates"
    assert settings.support == "https://t.me/support"
    assert settings.owner_link == "https://t.me/owner"


def test_settings_reports_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ["TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "MONGODB_URI"]:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="Missing required environment variables"):
        Settings.from_env()
