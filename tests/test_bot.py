from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pyrogram.enums import ParseMode

from autoreply.bot import (
    BOT_MENU_COMMANDS,
    START_TEXT,
    choose_reaction,
    command_argument,
    display_response_label,
    global_manager_keyboard,
    link_keyboard,
    manager_keyboard,
    message_label,
    response_label,
    saved_reply_keyboard,
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
        "updates",
        "support",
        "owner_link",
        "global_defaults",
    }


def test_disabled_parse_mode_preserves_angle_brackets() -> None:
    assert ParseMode.DISABLED.value == "disabled"


def test_start_text_contains_setup_steps() -> None:
    assert "/autoreply" in START_TEXT
    assert "private manager" in START_TEXT


def test_response_label_preserves_existing_text_responses() -> None:
    assert response_label("hello") == "hello"


def test_message_label_uses_text_preview() -> None:
    message = SimpleNamespace(text="Hello\nthere", caption=None, media=None)
    assert message_label(message) == "Hello there"
    assert response_label(
        {"kind": "message", "label": "Hello there", "has_preview": True}
    ) == "Hello there"


def test_message_label_uses_media_type_without_preview() -> None:
    message = SimpleNamespace(text=None, caption=None, media="MessageMediaType.PHOTO")
    assert message_label(message) == "photo"
    assert response_label({"kind": "message", "label": "photo", "has_preview": False}) == "[photo]"


@pytest.mark.asyncio
async def test_display_response_label_hydrates_legacy_text_entry() -> None:
    class FakeClient:
        async def get_messages(self, chat_id, message_id):
            return SimpleNamespace(text="Legacy text", caption=None, media=None)

    label = await display_response_label(
        FakeClient(),
        {"kind": "message", "chat_id": 123, "message_id": 42, "label": "text"},
    )

    assert label == "Legacy text"


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


def test_manager_keyboard_contains_private_controls() -> None:
    keyboard = manager_keyboard(
        -100123,
        {
            "enabled": False,
            "reactions_enabled": True,
            "reaction_chance": 25,
        },
    )
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "Add Reply" in labels
    assert "View Replies" in labels
    assert "Enable" in labels
    assert "Reaction Chance: 25%" in labels


def test_saved_reply_keyboard_contains_follow_up_actions() -> None:
    keyboard = saved_reply_keyboard(-100123)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert labels == ["Add Another Reply", "View Replies", "Back to Manager"]


def test_global_manager_keyboard_contains_owner_controls() -> None:
    keyboard = global_manager_keyboard()
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert labels == ["Add Global Reply", "View Global Replies", "Clear Global Replies"]
