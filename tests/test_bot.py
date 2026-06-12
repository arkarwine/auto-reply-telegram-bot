from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pyrogram.enums import ParseMode

from autoreply.bot import (
    BOT_MENU_COMMANDS,
    COMMAND_CATALOG,
    START_TEXT,
    choose_reaction,
    command_action,
    command_argument,
    link_keyboard,
    response_from_message,
    response_label,
    send_response,
    valid_link,
)


def test_command_argument_returns_remaining_text() -> None:
    message = SimpleNamespace(text="/autoreply add hello there")
    assert command_argument(message) == "add hello there"


def test_command_argument_handles_missing_argument() -> None:
    message = SimpleNamespace(text="/autoreply")
    assert command_argument(message) == ""


def test_command_argument_supports_autoreply_action() -> None:
    message = SimpleNamespace(text="/autoreply off")
    assert command_argument(message) == "off"


def test_command_action_splits_action_and_value() -> None:
    message = SimpleNamespace(text="/autoreply add hello there")
    assert command_action(message) == ("add", "hello there")


def test_choose_reaction_returns_none_when_chance_misses() -> None:
    with patch("autoreply.bot.random.randint", return_value=26):
        assert choose_reaction(25, ["👍"]) is None


def test_choose_reaction_selects_from_configured_reactions() -> None:
    with (
        patch("autoreply.bot.random.randint", return_value=25),
        patch("autoreply.bot.random.choice", return_value="🎉"),
    ):
        assert choose_reaction(25, ["👍", "🎉"]) == "🎉"


def test_choose_reaction_returns_none_for_empty_list() -> None:
    assert choose_reaction(100, []) is None


def test_bot_menu_contains_registered_commands() -> None:
    assert {command.command for command in BOT_MENU_COMMANDS} == {
        "start",
        "help",
        "autoreply",
        "reaction",
        "updates",
        "support",
        "owner_link",
    }


def test_disabled_parse_mode_preserves_angle_brackets() -> None:
    assert ParseMode.DISABLED.value == "disabled"


def test_autoreply_catalog_contains_reply_and_reaction_commands() -> None:
    assert "/autoreply add <message>" in COMMAND_CATALOG
    assert "/reaction chance <0-100>" in COMMAND_CATALOG


def test_start_text_contains_setup_steps() -> None:
    assert "/autoreply add <message>" in START_TEXT
    assert "/autoreply on" in START_TEXT


def test_response_from_replied_message() -> None:
    replied = SimpleNamespace(
        id=42,
        chat=SimpleNamespace(id=-100123),
        media="MessageMediaType.PHOTO",
    )
    command = SimpleNamespace(reply_to_message=replied)

    response = response_from_message(command)

    assert response == {
        "kind": "message",
        "chat_id": -100123,
        "message_id": 42,
        "label": "MessageMediaType.PHOTO",
    }
    assert response_label(response) == "[MessageMediaType.PHOTO]"


def test_response_label_preserves_existing_text_responses() -> None:
    assert response_label("hello") == "hello"


@pytest.mark.asyncio
async def test_send_response_copies_stored_telegram_message() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.arguments = None

        async def copy_message(self, **kwargs) -> None:
            self.arguments = kwargs

    client = FakeClient()
    incoming = SimpleNamespace(chat=SimpleNamespace(id=-100999), id=77)

    await send_response(
        client,
        incoming,
        {"kind": "message", "chat_id": -100123, "message_id": 42, "label": "photo"},
    )

    assert client.arguments == {
        "chat_id": -100999,
        "from_chat_id": -100123,
        "message_id": 42,
        "reply_to_message_id": 77,
    }


def test_link_keyboard_uses_configured_links() -> None:
    keyboard = link_keyboard(
        {
            "updates": "https://t.me/updates",
            "support": "https://t.me/support",
            "owner_link": "https://t.me/owner",
        }
    )

    assert keyboard is not None
    assert [row[0].url for row in keyboard.inline_keyboard] == [
        "https://t.me/updates",
        "https://t.me/support",
        "https://t.me/owner",
    ]


def test_link_keyboard_is_hidden_when_no_links_are_configured() -> None:
    assert link_keyboard({}) is None


def test_link_validation() -> None:
    assert valid_link("https://t.me/example")
    assert valid_link("tg://user?id=123")
    assert not valid_link("@example")
