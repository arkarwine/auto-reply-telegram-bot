import pytest

from autoreply.config import Settings, parse_id_list


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost")
    monkeypatch.setenv("OWNER_ID", "456")
    monkeypatch.setenv("SUDOER_IDS", "789, 101112 789")
    monkeypatch.setenv("STORAGE_CHAT_ID", "-100123")
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)

    settings = Settings.from_env()

    assert settings.api_id == 123
    assert settings.mongodb_database == "telegram_autoreply"
    assert settings.owner_id == 456
    assert settings.sudoer_ids == (789, 101112)
    assert settings.is_sudoer(456)
    assert settings.is_sudoer(789)
    assert not settings.is_sudoer(999)
    assert settings.storage_chat_id == -100123


def test_parse_id_list_accepts_commas_spaces_and_empty_values() -> None:
    assert parse_id_list(None) == ()
    assert parse_id_list("") == ()
    assert parse_id_list("1, 2 3,1") == (1, 2, 3)


def test_parse_id_list_rejects_non_integer_ids() -> None:
    with pytest.raises(RuntimeError, match="SUDOER_IDS"):
        parse_id_list("1, nope")


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
