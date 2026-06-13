from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ParseMode
from pyrogram.errors import FloodWait

from autoreply.bot import (
    HELP_TEXT,
    PUBLIC_BOT_COMMANDS,
    SUDOER_BOT_COMMANDS,
    SUDOER_HELP_TEXT,
    START_TEXT,
    broadcast_response,
    chance_succeeds,
    choose_reaction,
    command_argument,
    display_response_label,
    global_manager_keyboard,
    global_reply_list_content,
    group_onboarding_content,
    interaction_allowed,
    is_sudoer,
    link_keyboard,
    manager_keyboard,
    message_label,
    response_label,
    response_preview_text,
    reply_list_content,
    register_bot_commands,
    retry_flood_wait,
    saved_reply_keyboard,
    send_response,
    start_image_file_id,
    truncate_label,
    next_option,
    valid_link,
)
from autoreply.config import Settings


def test_command_argument_returns_remaining_text() -> None:
    message = SimpleNamespace(text="/autoreply add hello there")
    assert command_argument(message) == "add hello there"


def test_command_argument_handles_missing_argument() -> None:
    message = SimpleNamespace(text="/autoreply")
    assert command_argument(message) == ""


def test_command_argument_supports_autoreply_action() -> None:
    message = SimpleNamespace(text="/autoreply off")
    assert command_argument(message) == "off"


def test_is_sudoer_accepts_owner_and_extra_sudoers() -> None:
    settings = Settings(
        api_id=123,
        api_hash="hash",
        bot_token="token",
        mongodb_uri="mongodb://localhost",
        mongodb_database="telegram_autoreply",
        owner_id=1,
        sudoer_ids=(2, 3),
        storage_chat_id=None,
    )

    assert is_sudoer(settings, SimpleNamespace(from_user=SimpleNamespace(id=1)))
    assert is_sudoer(settings, SimpleNamespace(from_user=SimpleNamespace(id=2)))
    assert not is_sudoer(settings, SimpleNamespace(from_user=SimpleNamespace(id=4)))
    assert not is_sudoer(settings, SimpleNamespace(from_user=None))


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
    assert choose_reaction(100, ["", "   "]) is None


def test_reply_chance_check() -> None:
    with patch("autoreply.bot.random.randint", return_value=26):
        assert not chance_succeeds(25)
    with patch("autoreply.bot.random.randint", return_value=25):
        assert chance_succeeds(25)


def test_interaction_cooldown_and_rate_limit() -> None:
    from autoreply import bot

    bot._last_interaction.clear()
    bot._recent_interactions.clear()
    assert interaction_allowed(1, cooldown=5, per_minute=2, now=100)
    assert not interaction_allowed(1, cooldown=5, per_minute=2, now=102)
    assert interaction_allowed(1, cooldown=5, per_minute=2, now=106)
    assert not interaction_allowed(1, cooldown=0, per_minute=2, now=107)
    assert interaction_allowed(1, cooldown=0, per_minute=2, now=161)


def test_next_option_cycles_manager_values() -> None:
    assert next_option(5, [0, 5, 15]) == 15
    assert next_option(15, [0, 5, 15]) == 0


@pytest.mark.asyncio
async def test_flood_wait_is_retried() -> None:
    attempts = 0

    async def action():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise FloodWait(0)
        return "ok"

    assert await retry_flood_wait(action) == "ok"
    assert attempts == 2


def test_public_bot_menu_hides_sudoer_commands() -> None:
    assert {command.command for command in PUBLIC_BOT_COMMANDS} == {
        "start",
        "help",
        "autoreply",
    }
    assert {command.command for command in SUDOER_BOT_COMMANDS} == {
        "start",
        "help",
        "autoreply",
        "updates",
        "support",
        "owner_link",
        "global_defaults",
        "broadcast",
        "start_img",
    }


@pytest.mark.asyncio
async def test_bot_commands_are_registered_for_each_sudoer() -> None:
    class FakeApp:
        def __init__(self):
            self.calls = []

        async def set_bot_commands(self, commands, scope):
            self.calls.append((commands, scope))

    app = FakeApp()
    settings = Settings(
        api_id=123,
        api_hash="hash",
        bot_token="token",
        mongodb_uri="mongodb://localhost",
        mongodb_database="telegram_autoreply",
        owner_id=1,
        sudoer_ids=(2, 3),
        storage_chat_id=None,
    )

    await register_bot_commands(app, settings)

    assert [getattr(scope, "chat_id", None) for _, scope in app.calls] == [None, 1, 2, 3]
    assert app.calls[0][0] == PUBLIC_BOT_COMMANDS
    assert all(commands == SUDOER_BOT_COMMANDS for commands, _ in app.calls[1:])


def test_disabled_parse_mode_preserves_angle_brackets() -> None:
    assert ParseMode.DISABLED.value == "disabled"


def test_start_text_contains_setup_steps() -> None:
    assert "/autoreply" in START_TEXT
    assert "✨ Auto Reply" in START_TEXT
    assert "/global_defaults" not in HELP_TEXT
    assert "/global_defaults" in SUDOER_HELP_TEXT
    assert "/broadcast" in SUDOER_HELP_TEXT


@pytest.mark.asyncio
async def test_broadcast_response_copies_to_every_known_group() -> None:
    class FakeRepository:
        async def group_ids(self):
            return list(range(21))

        async def set_enabled(self, chat_id, enabled):
            raise AssertionError("No group should be disabled")

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def copy_message(self, **kwargs):
            self.calls.append(kwargs)

    client = FakeClient()
    with patch("autoreply.bot.asyncio.sleep") as sleep:
        sent, failed = await broadcast_response(
            client,
            FakeRepository(),
            {"chat_id": 123, "message_id": 42},
        )

    assert (sent, failed) == (21, 0)
    assert [call["chat_id"] for call in client.calls] == list(range(21))
    sleep.assert_awaited_once_with(3)


def test_response_label_preserves_existing_text_responses() -> None:
    assert response_label("hello") == "hello"


def test_reply_labels_are_truncated() -> None:
    assert truncate_label("short") == "short"
    assert truncate_label("x" * 50, limit=10) == "xxxxxxx..."


@pytest.mark.asyncio
async def test_reply_list_shows_ten_truncated_items_with_preview_actions() -> None:
    class FakeRepository:
        async def get(self, chat_id):
            return {
                "responses": [f"local reply {index} " + "x" * 50 for index in range(1, 12)],
                "excluded_global_responses": [],
            }

        async def get_global_responses(self):
            return []

    text, keyboard = await reply_list_content(SimpleNamespace(), FakeRepository(), -100123, 0)

    assert "local reply 10" in text
    assert "local reply 11" not in text
    assert "..." in text
    assert len(keyboard.inline_keyboard) == 12
    assert [button.text for button in keyboard.inline_keyboard[0]] == ["👁 L1", "🗑 L1"]
    assert keyboard.inline_keyboard[0][0].callback_data == "mgr:preview-l-1-0:-100123"
    assert keyboard.inline_keyboard[0][1].callback_data == "mgr:delete-1-0:-100123"


@pytest.mark.asyncio
async def test_global_reply_list_has_preview_next_to_delete() -> None:
    class FakeRepository:
        async def get_global_responses(self):
            return ["one", "two"]

    _, keyboard = await global_reply_list_content(SimpleNamespace(), FakeRepository(), 0)

    assert [button.text for button in keyboard.inline_keyboard[0]] == ["👁 1", "🗑 1"]
    assert keyboard.inline_keyboard[0][0].callback_data == "global:preview-1-0"


@pytest.mark.asyncio
async def test_response_preview_text_is_suitable_for_in_place_menu() -> None:
    text = await response_preview_text(SimpleNamespace(), "full reply text", "L1")

    assert text == "👁 L1\n\nfull reply text"


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
        "reply_parameters": client.arguments["reply_parameters"],
    }
    assert client.arguments["reply_parameters"].message_id == 77


def test_link_keyboard_uses_configured_links() -> None:
    keyboard = link_keyboard(
        {
            "updates": "https://t.me/updates",
            "support": "https://t.me/support",
            "owner_link": "https://t.me/owner",
        },
        "example_bot",
    )

    assert keyboard is not None
    assert [button.url for row in keyboard.inline_keyboard for button in row if button.url] == [
        "https://t.me/example_bot?startgroup=true",
        "https://t.me/updates",
        "https://t.me/support",
        "https://t.me/owner",
    ]
    assert [[button.text for button in row] for row in keyboard.inline_keyboard] == [
        ["➕ Add to Group"],
        ["❓ Help", "📢 Updates"],
        ["💬 Support", "👤 Owner"],
    ]
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/example_bot?startgroup=true"
    styles = {
        button.text: button.style for row in keyboard.inline_keyboard for button in row
    }
    assert styles["➕ Add to Group"] == ButtonStyle.SUCCESS
    assert all(
        styles[label] == ButtonStyle.DEFAULT
        for label in ("❓ Help", "📢 Updates", "💬 Support", "👤 Owner")
    )


def test_link_keyboard_keeps_help_when_no_links_are_configured() -> None:
    keyboard = link_keyboard({})
    assert keyboard is not None
    assert [row[0].text for row in keyboard.inline_keyboard] == ["❓ Help"]


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
            "reply_chance": 75,
            "reaction_chance": 25,
            "cooldown_seconds": 0,
            "rate_limit_per_minute": 0,
            "global_replies_enabled": True,
        },
    )
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "➕ Add Reply" in labels
    assert "📚 Replies" in labels
    assert "▶️ Enable" in labels
    assert "💬 Reply: 75%" in labels
    assert "🎲 React: 25%" in labels
    assert "⏱ 0s" in labels
    assert "🚦 ∞/min" in labels
    assert "🌐 Globals: On" in labels
    styles = {button.text: button.style for row in keyboard.inline_keyboard for button in row}
    assert styles["➕ Add Reply"] == ButtonStyle.SUCCESS
    assert styles["▶️ Enable"] == ButtonStyle.SUCCESS
    assert styles["🗑 Clear Replies"] == ButtonStyle.DANGER


def test_saved_reply_keyboard_contains_follow_up_actions() -> None:
    keyboard = saved_reply_keyboard(-100123)
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert labels == ["➕ Add Another", "📚 Replies", "⬅️ Manager"]


def test_global_manager_keyboard_contains_owner_controls() -> None:
    keyboard = global_manager_keyboard()
    labels = [button.text for row in keyboard.inline_keyboard for button in row]

    assert labels == ["➕ Add Global", "🌐 Replies", "🗑 Clear Globals"]


@pytest.mark.asyncio
async def test_group_onboarding_mentions_only_unmet_requirements() -> None:
    class FakeRepository:
        async def set_enabled(self, chat_id, enabled):
            assert (chat_id, enabled) == (-100123, True)

    class FakeClient:
        async def get_me(self):
            return SimpleNamespace(id=1, username="bot")

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)

    message = SimpleNamespace(chat=SimpleNamespace(id=-100123))
    text, keyboard = await group_onboarding_content(FakeClient(), FakeRepository(), message)

    assert "Promote me" not in text
    assert "OK" not in text
    assert "Disable privacy mode" in text
    assert "Add replies" in text
    assert keyboard.inline_keyboard[0][0].text == "⚙️ Open Manager"


def test_start_image_file_id_accepts_attached_or_replied_photo() -> None:
    attached = SimpleNamespace(
        photo=SimpleNamespace(file_id="attached"),
        reply_to_message=None,
    )
    replied = SimpleNamespace(
        photo=None,
        reply_to_message=SimpleNamespace(photo=SimpleNamespace(file_id="replied")),
    )

    assert start_image_file_id(attached) == "attached"
    assert start_image_file_id(replied) == "replied"
