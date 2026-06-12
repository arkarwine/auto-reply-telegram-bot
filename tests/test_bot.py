from types import SimpleNamespace
from unittest.mock import patch

from pyrogram.enums import ParseMode

from autoreply.bot import (
    BOT_MENU_COMMANDS,
    COMMAND_CATALOG,
    START_TEXT,
    choose_reaction,
    command_action,
    command_argument,
    configured_link,
    link_keyboard,
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


def test_link_keyboard_uses_configured_links() -> None:
    settings = SimpleNamespace(
        updates="https://t.me/updates",
        support="https://t.me/support",
        owner_link="https://t.me/owner",
    )

    keyboard = link_keyboard(settings)

    assert keyboard is not None
    assert [row[0].url for row in keyboard.inline_keyboard] == [
        "https://t.me/updates",
        "https://t.me/support",
        "https://t.me/owner",
    ]
    assert configured_link(settings, "owner_link") == ("Owner", "https://t.me/owner")
