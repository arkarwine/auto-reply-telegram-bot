from types import SimpleNamespace
from unittest.mock import patch

from autoreply.bot import BOT_MENU_COMMANDS, choose_reaction, command_argument


def test_command_argument_returns_remaining_text() -> None:
    message = SimpleNamespace(text="/autoreply_add hello there")
    assert command_argument(message) == "hello there"


def test_command_argument_handles_missing_argument() -> None:
    message = SimpleNamespace(text="/autoreply_add")
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
        "autoreply_add",
        "autoreply_remove",
        "autoreply_list",
        "autoreply_clear",
        "autoreply_status",
        "autoreply_help",
        "reaction_on",
        "reaction_off",
        "reaction_chance",
        "reaction_add",
        "reaction_remove",
        "reaction_list",
    }
