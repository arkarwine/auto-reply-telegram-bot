from types import SimpleNamespace
from unittest.mock import patch

from autoreply.bot import choose_reaction, command_argument


def test_command_argument_returns_remaining_text() -> None:
    message = SimpleNamespace(text="/autoreply_add hello there")
    assert command_argument(message) == "hello there"


def test_command_argument_handles_missing_argument() -> None:
    message = SimpleNamespace(text="/autoreply_add")
    assert command_argument(message) == ""


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
