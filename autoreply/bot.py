import logging
import random
import asyncio

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from autoreply.config import Settings
from autoreply.repository import GroupRepository, MAX_RESPONSES


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)
COMMANDS = [
    "autoreply",
    "reaction",
]
BOT_MENU_COMMANDS = [
    BotCommand("start", "Open the bot guide"),
    BotCommand("help", "Show setup and usage help"),
    BotCommand("autoreply", "Open the private group manager"),
    BotCommand("updates", "Owner: configure updates button"),
    BotCommand("support", "Owner: configure support button"),
    BotCommand("owner_link", "Owner: configure owner button"),
    BotCommand("global_defaults", "Owner: manage global default replies"),
]
START_TEXT = (
    "Telegram Group Interaction Bot\n\n"
    "I can keep group chats active with rotating automatic replies and occasional random reactions.\n\n"
    "Quick setup:\n"
    "1. Add me to your group as an administrator.\n"
    "2. Disable privacy mode through BotFather so I can see group messages.\n"
    "3. Send /autoreply in the group.\n"
    "4. Open the private manager to add replies and enable interactions.\n\n"
    "All configuration happens privately, keeping the group clean."
)
MANAGER_DELETE_DELAY = 30


def command_argument(message: Message) -> str:
    text = message.text or ""
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


def choose_reaction(chance: int, reactions: list[str]) -> str | None:
    if not reactions or random.randint(1, 100) > chance:
        return None
    return random.choice(reactions)


def chance_succeeds(chance: int) -> bool:
    return random.randint(1, 100) <= chance


def link_keyboard(links: dict[str, str]) -> InlineKeyboardMarkup | None:
    buttons = []
    if links.get("updates"):
        buttons.append(InlineKeyboardButton("Updates Channel", url=links["updates"]))
    if links.get("support"):
        buttons.append(InlineKeyboardButton("Support Group", url=links["support"]))
    if links.get("owner_link"):
        buttons.append(InlineKeyboardButton("Owner", url=links["owner_link"]))
    return InlineKeyboardMarkup([[button] for button in buttons]) if buttons else None


def valid_link(value: str) -> bool:
    return value.startswith(("https://", "http://", "tg://"))


def message_label(message: Message, limit: int = 80) -> str:
    preview = " ".join((message.text or message.caption or "").split())
    if preview:
        return preview if len(preview) <= limit else preview[: limit - 3] + "..."
    media = str(message.media or "message")
    return media.removeprefix("MessageMediaType.").lower()


def response_label(response: object) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict) and response.get("kind") == "message":
        label = response.get("label", "message")
        return label if response.get("has_preview") else f"[{label}]"
    return "[unsupported response]"


async def display_response_label(client: Client, response: object) -> str:
    if (
        isinstance(response, dict)
        and response.get("kind") == "message"
        and response.get("label") == "text"
        and not response.get("has_preview")
    ):
        try:
            source = await client.get_messages(response["chat_id"], response["message_id"])
            if source:
                return message_label(source)
        except (KeyError, RPCError):
            pass
    return response_label(response)


async def send_response(client: Client, incoming: Message, response: object) -> None:
    if isinstance(response, str):
        await incoming.reply_text(response)
        return
    if isinstance(response, dict) and response.get("kind") == "message":
        await client.copy_message(
            chat_id=incoming.chat.id,
            from_chat_id=response["chat_id"],
            message_id=response["message_id"],
            reply_to_message_id=incoming.id,
        )
        return
    raise ValueError("Unsupported stored response")


async def is_group_admin(client: Client, message: Message) -> bool:
    if not message.from_user:
        return False
    member = await client.get_chat_member(message.chat.id, message.from_user.id)
    return member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}


async def user_is_group_admin(client: Client, chat_id: int, user_id: int) -> bool:
    try:
        member = await client.get_chat_member(chat_id, user_id)
    except RPCError:
        return False
    return member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}


async def delete_later(*messages: Message) -> None:
    await asyncio.sleep(MANAGER_DELETE_DELAY)
    for message in messages:
        try:
            await message.delete()
        except RPCError:
            pass


def manager_keyboard(chat_id: int, document: dict) -> InlineKeyboardMarkup:
    enabled_label = "Disable" if document["enabled"] else "Enable"
    reactions_label = "Reactions Off" if document.get("reactions_enabled", True) else "Reactions On"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add Reply", callback_data=f"mgr:add:{chat_id}"),
                InlineKeyboardButton("View Replies", callback_data=f"mgr:list:{chat_id}"),
            ],
            [
                InlineKeyboardButton(enabled_label, callback_data=f"mgr:toggle:{chat_id}"),
                InlineKeyboardButton(reactions_label, callback_data=f"mgr:reactions:{chat_id}"),
            ],
            [
                InlineKeyboardButton(
                    f"Reply Chance: {document.get('reply_chance', 100)}%",
                    callback_data=f"mgr:reply-chance:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"Reaction Chance: {document.get('reaction_chance', 25)}%",
                    callback_data=f"mgr:chance:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton("Clear Local Replies", callback_data=f"mgr:clear:{chat_id}"),
                InlineKeyboardButton("Refresh", callback_data=f"mgr:open:{chat_id}"),
            ],
        ]
    )


def saved_reply_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add Another Reply", callback_data=f"mgr:add:{chat_id}"),
                InlineKeyboardButton("View Replies", callback_data=f"mgr:list:{chat_id}"),
            ],
            [InlineKeyboardButton("Back to Manager", callback_data=f"mgr:open:{chat_id}")],
        ]
    )


def global_manager_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add Global Reply", callback_data="global:add"),
                InlineKeyboardButton("View Global Replies", callback_data="global:list"),
            ],
            [InlineKeyboardButton("Clear Global Replies", callback_data="global:clear")],
        ]
    )


def global_saved_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Add Another", callback_data="global:add"),
                InlineKeyboardButton("View Global Replies", callback_data="global:list"),
            ],
            [InlineKeyboardButton("Back", callback_data="global:open")],
        ]
    )


async def global_manager_content(repository: GroupRepository) -> tuple[str, InlineKeyboardMarkup]:
    responses = await repository.get_global_responses()
    return (
        "Global Default Replies\n\n"
        f"Replies: {len(responses)}\n\n"
        "Included in the reply rotation of every enabled group.",
        global_manager_keyboard(),
    )


async def manager_content(client: Client, repository: GroupRepository, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    chat = await client.get_chat(chat_id)
    document = await repository.get(chat_id)
    local_count = len(document["responses"])
    global_count = len(await repository.get_global_responses())
    text = (
        "Auto Reply Manager\n\n"
        f"Group: {chat.title or chat_id}\n"
        f"Interactions: {'enabled' if document['enabled'] else 'disabled'}\n"
        f"Replies: {local_count} local + {global_count} global\n"
        f"Reply chance: {document.get('reply_chance', 100)}%\n"
        f"Reactions: {'enabled' if document.get('reactions_enabled', True) else 'disabled'}\n"
        f"Reaction chance: {document.get('reaction_chance', 25)}%"
    )
    return text, manager_keyboard(chat_id, document)


async def open_manager(client: Client, repository: GroupRepository, message: Message, chat_id: int) -> None:
    if not message.from_user or not await user_is_group_admin(client, chat_id, message.from_user.id):
        await message.reply_text("You are not an administrator of that group.")
        return
    text, keyboard = await manager_content(client, repository, chat_id)
    await message.reply_text(text, reply_markup=keyboard)


async def require_admin(client: Client, message: Message) -> bool:
    try:
        allowed = await is_group_admin(client, message)
    except RPCError as exc:
        LOGGER.warning("Could not verify admin in chat %s: %s", message.chat.id, exc)
        await message.reply_text(
            "I could not verify group administrators. Make me a group administrator, then try again."
        )
        return False
    if not allowed:
        await message.reply_text("Only group administrators can manage auto-replies.")
    return allowed


def register_handlers(app: Client, repository: GroupRepository, settings: Settings) -> None:
    @app.on_message(filters.private & filters.command(["start", "help"]))
    async def private_help(client: Client, message: Message) -> None:
        argument = command_argument(message)
        if argument.startswith("configure_"):
            try:
                chat_id = int(argument.removeprefix("configure_"))
            except ValueError:
                await message.reply_text("Invalid group configuration link.")
                return
            await open_manager(client, repository, message, chat_id)
            return
        links = await repository.get_links()
        await message.reply_text(START_TEXT, reply_markup=link_keyboard(links))

    @app.on_message(filters.private & filters.command(["autoreply", "reaction"]))
    async def private_manager_hint(_: Client, message: Message) -> None:
        await message.reply_text(
            "Send /autoreply in the group you want to configure, then open the private manager button."
        )

    @app.on_message(filters.private & filters.command("global_defaults"))
    async def global_defaults_command(_: Client, message: Message) -> None:
        if not message.from_user or message.from_user.id != settings.owner_id:
            await message.reply_text("Only the bot owner can manage global default replies.")
            return
        text, keyboard = await global_manager_content(repository)
        await message.reply_text(text, reply_markup=keyboard)

    @app.on_message(filters.private & filters.command(["updates", "support", "owner_link"]))
    async def link_command(_: Client, message: Message) -> None:
        if not message.from_user or message.from_user.id != settings.owner_id:
            await message.reply_text("Only the bot owner can configure start-menu links.")
            return

        name = message.command[0].lower()
        value = command_argument(message)
        labels = {"updates": "Updates channel", "support": "Support group", "owner_link": "Owner"}
        if not value:
            current = (await repository.get_links()).get(name, "not configured")
            await message.reply_text(f"{labels[name]}: {current}\nUsage: /{name} <url> or /{name} off")
        elif value.lower() == "off":
            await repository.set_link(name, None)
            await message.reply_text(f"{labels[name]} button removed.")
        elif not valid_link(value):
            await message.reply_text("Link must start with https://, http://, or tg://")
        else:
            await repository.set_link(name, value)
            await message.reply_text(f"{labels[name]} button updated.")

    group_commands = filters.group & filters.command(COMMANDS)

    @app.on_message(group_commands)
    async def handle_command(client: Client, message: Message) -> None:
        if not await require_admin(client, message):
            return
        me = await client.get_me()
        launcher = await message.reply_text(
            "Configure this group privately.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Open Auto Reply Manager", url=f"https://t.me/{me.username}?start=configure_{message.chat.id}")]]
            ),
        )
        asyncio.create_task(delete_later(message, launcher))

    @app.on_callback_query(filters.regex(r"^mgr:"))
    async def manager_callback(client: Client, query: CallbackQuery) -> None:
        if not query.from_user or not query.message:
            return
        try:
            _, action, raw_chat_id = query.data.split(":", 2)
            chat_id = int(raw_chat_id)
        except (AttributeError, ValueError):
            await query.answer("Invalid manager action.", show_alert=True)
            return
        if not await user_is_group_admin(client, chat_id, query.from_user.id):
            await query.answer("You are no longer an administrator of this group.", show_alert=True)
            return

        if action == "add":
            await repository.set_capture_group(query.from_user.id, chat_id)
            await query.message.reply_text(
                "Send any message now. Text, photos, videos, stickers, documents, voice notes, polls, and other copyable messages are supported.\n\nSend /cancel to stop."
            )
            await query.answer("Waiting for a reply message.")
            return
        if action == "toggle":
            document = await repository.get(chat_id)
            await repository.set_enabled(chat_id, not document["enabled"])
        elif action == "reactions":
            document = await repository.get(chat_id)
            await repository.set_reactions_enabled(
                chat_id, not document.get("reactions_enabled", True)
            )
        elif action == "chance":
            document = await repository.get(chat_id)
            chance = (document.get("reaction_chance", 25) + 25) % 125
            await repository.set_reaction_chance(chat_id, chance)
        elif action == "reply-chance":
            document = await repository.get(chat_id)
            chance = (document.get("reply_chance", 100) + 25) % 125
            await repository.set_reply_chance(chat_id, chance)
        elif action == "clear":
            await repository.clear_responses(chat_id)
        elif action.startswith("delete-"):
            try:
                index = int(action.removeprefix("delete-"))
            except ValueError:
                await query.answer("Invalid reply number.", show_alert=True)
                return
            await repository.remove_response(chat_id, index)
        elif action == "list":
            document = await repository.get(chat_id)
            local_responses = document["responses"]
            global_responses = await repository.get_global_responses()
            local_labels = [
                await display_response_label(client, response) for response in local_responses
            ]
            global_labels = [
                await display_response_label(client, response) for response in global_responses
            ]
            sections = []
            if local_labels:
                sections.append(
                    "Local replies:\n"
                    + "\n".join(f"{index}. {label}" for index, label in enumerate(local_labels, 1))
                )
            if global_labels:
                sections.append(
                    "Global replies (read-only):\n"
                    + "\n".join(f"G{index}. {label}" for index, label in enumerate(global_labels, 1))
                )
            text = "\n\n".join(sections) if sections else "No replies configured."
            buttons = [
                [InlineKeyboardButton(f"Delete {index}", callback_data=f"mgr:delete-{index}:{chat_id}")]
                for index in range(1, min(len(local_responses), 20) + 1)
            ]
            buttons.append([InlineKeyboardButton("Back", callback_data=f"mgr:open:{chat_id}")])
            await query.message.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return

        text, keyboard = await manager_content(client, repository, chat_id)
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer("Updated.")

    @app.on_callback_query(filters.regex(r"^global:"))
    async def global_callback(client: Client, query: CallbackQuery) -> None:
        if not query.from_user or not query.message or query.from_user.id != settings.owner_id:
            await query.answer("Only the bot owner can manage global replies.", show_alert=True)
            return
        action = query.data.split(":", 1)[1]
        if action == "add":
            await repository.set_global_capture(query.from_user.id)
            await query.message.reply_text(
                "Send any message now to save it as a global default reply.\n\nSend /cancel to stop."
            )
            await query.answer("Waiting for a global reply.")
            return
        if action == "clear":
            await repository.clear_global_responses()
        elif action.startswith("delete-"):
            try:
                index = int(action.removeprefix("delete-"))
            except ValueError:
                await query.answer("Invalid reply number.", show_alert=True)
                return
            await repository.remove_global_response(index)
        elif action == "list":
            responses = await repository.get_global_responses()
            labels = [await display_response_label(client, response) for response in responses]
            text = (
                "No global default replies configured."
                if not responses
                else "Global default replies:\n"
                + "\n".join(f"{index}. {label}" for index, label in enumerate(labels, 1))
            )
            buttons = [
                [InlineKeyboardButton(f"Delete {index}", callback_data=f"global:delete-{index}")]
                for index in range(1, min(len(responses), 20) + 1)
            ]
            buttons.append([InlineKeyboardButton("Back", callback_data="global:open")])
            await query.message.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        text, keyboard = await global_manager_content(repository)
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer("Updated.")

    @app.on_message(filters.private & filters.command("cancel"))
    async def cancel_capture(_: Client, message: Message) -> None:
        if message.from_user:
            await repository.clear_capture_group(message.from_user.id)
        await message.reply_text("Reply capture cancelled.")

    @app.on_message(
        filters.private
        & ~filters.command(
            [
                "start",
                "help",
                "cancel",
                "updates",
                "support",
                "owner_link",
                "autoreply",
                "reaction",
                "global_defaults",
            ]
        ),
        group=1,
    )
    async def capture_private_message(client: Client, message: Message) -> None:
        if not message.from_user:
            return
        global_capture = (
            message.from_user.id == settings.owner_id
            and await repository.is_global_capture(message.from_user.id)
        )
        chat_id = await repository.get_capture_group(message.from_user.id)
        if chat_id is None and not global_capture:
            return
        if not global_capture and not await user_is_group_admin(client, chat_id, message.from_user.id):
            await repository.clear_capture_group(message.from_user.id)
            await message.reply_text("You are no longer an administrator of that group.")
            return
        source = message
        if settings.storage_chat_id:
            try:
                source = await client.copy_message(settings.storage_chat_id, message.chat.id, message.id)
            except RPCError:
                LOGGER.exception("Could not copy captured reply to storage chat")
                await message.reply_text(
                    "I could not save that message to the storage chat. Check STORAGE_CHAT_ID and my permissions."
                )
                return
        response = {
            "kind": "message",
            "chat_id": source.chat.id,
            "message_id": source.id,
            "label": message_label(message),
            "has_preview": bool(message.text or message.caption),
        }
        result = (
            await repository.add_global_response(response)
            if global_capture
            else await repository.add_response(chat_id, response)
        )
        await repository.clear_capture_group(message.from_user.id)
        replies = {
            "added": "Reply saved.",
            "duplicate": "That reply is already configured.",
            "full": f"This group already has the maximum of {MAX_RESPONSES} replies.",
        }
        if global_capture:
            replies["added"] = "Global default reply saved."
        await message.reply_text(
            replies[result],
            reply_markup=(
                global_saved_keyboard()
                if global_capture and result == "added"
                else saved_reply_keyboard(chat_id)
                if result == "added"
                else None
            ),
        )

    async def eligible_message(_, __, message: Message) -> bool:
        return bool(
            message.from_user
            and not message.from_user.is_bot
            and message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
            and not (message.text or "").startswith("/")
            and not message.service
        )

    @app.on_message(filters.create(eligible_message), group=1)
    async def handle_group_message(client: Client, message: Message) -> None:
        reply_chance = await repository.reply_chance(message.chat.id)
        response = (
            await repository.next_response(message.chat.id)
            if reply_chance is not None and chance_succeeds(reply_chance)
            else None
        )
        if response:
            try:
                await send_response(client, message, response)
            except FloodWait as exc:
                LOGGER.warning("Reply flood wait for %s seconds in chat %s", exc.value, message.chat.id)
            except RPCError:
                LOGGER.exception("Could not reply in chat %s", message.chat.id)
            except (KeyError, TypeError, ValueError):
                LOGGER.exception("Invalid stored response in chat %s", message.chat.id)

        reaction_settings = await repository.reaction_settings(message.chat.id)
        reaction = choose_reaction(*reaction_settings) if reaction_settings else None
        if reaction:
            try:
                await message.react(reaction)
            except FloodWait as exc:
                LOGGER.warning(
                    "Reaction flood wait for %s seconds in chat %s", exc.value, message.chat.id
                )
            except RPCError:
                LOGGER.exception("Could not react in chat %s", message.chat.id)


async def start() -> None:
    settings = Settings.from_env()
    repository = GroupRepository(settings.mongodb_uri, settings.mongodb_database)
    await repository.ping()

    app = Client(
        "autoreply_bot",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        in_memory=True,
        parse_mode=ParseMode.DISABLED,
    )
    register_handlers(app, repository, settings)

    try:
        await app.start()
        await app.set_bot_commands(BOT_MENU_COMMANDS)
        LOGGER.info("Interaction bot started and command menu registered")
        await idle()
    finally:
        await app.stop()
        await repository.close()


def run() -> None:
    import asyncio

    asyncio.run(start())
