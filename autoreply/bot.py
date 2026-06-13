import logging
import random
import asyncio
from collections import defaultdict, deque
from time import monotonic
from collections.abc import Awaitable, Callable
from typing import TypeVar

from pyrogram import Client, filters, idle
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import ChatAdminRequired, FloodWait, Forbidden, RPCError, ReactionInvalid
from pyrogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)

from autoreply.config import Settings
from autoreply.repository import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    GroupRepository,
    MAX_RESPONSES,
)


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
    BotCommand("start_img", "Owner: configure start-menu image"),
]
START_TEXT = (
    "Telegram Group Interaction Bot\n\n"
    "I can keep group chats active with random automatic replies and occasional random reactions.\n\n"
    "Quick setup:\n"
    "1. Add me to your group as an administrator.\n"
    "2. Disable privacy mode through BotFather so I can see group messages.\n"
    "3. Send /autoreply in the group.\n"
    "4. Open the private manager to add replies and tune interactions.\n\n"
    "All configuration happens privately, keeping the group clean."
)
HELP_TEXT = (
    "Auto Reply Help\n\n"
    "Group setup:\n"
    "- Add me to a group as an administrator.\n"
    "- In BotFather, disable privacy mode so I can see normal group messages.\n"
    "- Send /autoreply in the group and open the private manager.\n\n"
    "Manager controls:\n"
    "- Add any copyable Telegram message as a reply.\n"
    "- Set reply chance, reaction chance, cooldown, and per-minute rate limit.\n"
    "- Include or exclude global default replies per group.\n\n"
    "Owner controls:\n"
    "- /global_defaults manages global default replies.\n"
    "- /updates, /support, and /owner_link configure start-menu buttons.\n"
    "- /start_img configures the start-menu image."
)
MANAGER_DELETE_DELAY = 30
REPLIES_PER_PAGE = 5
COOLDOWN_OPTIONS = [0, 5, 15, 30, 60]
RATE_LIMIT_OPTIONS = [0, 5, 10, 20, 30]
_last_interaction: dict[int, float] = {}
_recent_interactions: dict[int, deque[float]] = defaultdict(deque)
T = TypeVar("T")


def command_argument(message: Message) -> str:
    text = message.text or ""
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


def choose_reaction(chance: int, reactions: list[str]) -> str | None:
    reactions = [reaction.strip() for reaction in reactions if reaction and reaction.strip()]
    if not reactions or random.randint(1, 100) > chance:
        return None
    return random.choice(reactions)


def chance_succeeds(chance: int) -> bool:
    return random.randint(1, 100) <= chance


def next_option(current: int, options: list[int]) -> int:
    try:
        return options[(options.index(current) + 1) % len(options)]
    except ValueError:
        return options[0]


def interaction_allowed(chat_id: int, cooldown: int, per_minute: int, now: float | None = None) -> bool:
    current = monotonic() if now is None else now
    if cooldown and current - _last_interaction.get(chat_id, float("-inf")) < cooldown:
        return False
    recent = _recent_interactions[chat_id]
    while recent and current - recent[0] >= 60:
        recent.popleft()
    if per_minute and len(recent) >= per_minute:
        return False
    _last_interaction[chat_id] = current
    recent.append(current)
    return True


async def retry_flood_wait(action: Callable[[], Awaitable[T]], retries: int = 2) -> T:
    for attempt in range(retries + 1):
        try:
            return await action()
        except FloodWait as exc:
            if attempt == retries:
                raise
            await asyncio.sleep(exc.value)
    raise RuntimeError("unreachable")


def link_keyboard(links: dict[str, str]) -> InlineKeyboardMarkup | None:
    buttons = [
        InlineKeyboardButton("Help", callback_data="start:help", style=ButtonStyle.PRIMARY)
    ]
    if links.get("updates"):
        buttons.append(
            InlineKeyboardButton("Updates Channel", url=links["updates"], style=ButtonStyle.PRIMARY)
        )
    if links.get("support"):
        buttons.append(
            InlineKeyboardButton("Support Group", url=links["support"], style=ButtonStyle.SUCCESS)
        )
    if links.get("owner_link"):
        buttons.append(InlineKeyboardButton("Owner", url=links["owner_link"]))
    return InlineKeyboardMarkup([[button] for button in buttons]) if buttons else None


def valid_link(value: str) -> bool:
    return value.startswith(("https://", "http://", "tg://"))


def is_sudoer(settings: Settings, message: Message) -> bool:
    return bool(message.from_user and settings.is_sudoer(message.from_user.id))


def query_is_sudoer(settings: Settings, query: CallbackQuery) -> bool:
    return bool(query.from_user and settings.is_sudoer(query.from_user.id))


def start_image_file_id(message: Message) -> str | None:
    source = message if message.photo else message.reply_to_message
    return source.photo.file_id if source and source.photo else None


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
    reply_parameters = ReplyParameters(message_id=incoming.id)
    if isinstance(response, str):
        await client.send_message(
            chat_id=incoming.chat.id,
            text=response,
            reply_parameters=reply_parameters,
        )
        return
    if isinstance(response, dict) and response.get("kind") == "message":
        await client.copy_message(
            chat_id=incoming.chat.id,
            from_chat_id=response["chat_id"],
            message_id=response["message_id"],
            reply_parameters=reply_parameters,
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
                InlineKeyboardButton(
                    "Add Reply", callback_data=f"mgr:add:{chat_id}", style=ButtonStyle.SUCCESS
                ),
                InlineKeyboardButton(
                    "View Replies", callback_data=f"mgr:list:{chat_id}", style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    enabled_label,
                    callback_data=f"mgr:toggle:{chat_id}",
                    style=ButtonStyle.DANGER if document["enabled"] else ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    reactions_label,
                    callback_data=f"mgr:reactions:{chat_id}",
                    style=(
                        ButtonStyle.DANGER
                        if document.get("reactions_enabled", True)
                        else ButtonStyle.SUCCESS
                    ),
                ),
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
                InlineKeyboardButton(
                    f"Cooldown: {document.get('cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)}s",
                    callback_data=f"mgr:cooldown:{chat_id}",
                ),
                InlineKeyboardButton(
                    f"Rate: {document.get('rate_limit_per_minute', DEFAULT_RATE_LIMIT_PER_MINUTE) or 'Unlimited'}/min",
                    callback_data=f"mgr:rate:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Global Replies On"
                    if document.get("global_replies_enabled", True)
                    else "Global Replies Off",
                    callback_data=f"mgr:globals:{chat_id}",
                    style=(
                        ButtonStyle.DANGER
                        if document.get("global_replies_enabled", True)
                        else ButtonStyle.SUCCESS
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "Clear Local Replies",
                    callback_data=f"mgr:confirm-clear:{chat_id}",
                    style=ButtonStyle.DANGER,
                ),
                InlineKeyboardButton("Refresh", callback_data=f"mgr:open:{chat_id}"),
            ],
        ]
    )


def saved_reply_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Add Another Reply",
                    callback_data=f"mgr:add:{chat_id}",
                    style=ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    "View Replies", callback_data=f"mgr:list:{chat_id}", style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "Back to Manager",
                    callback_data=f"mgr:open:{chat_id}",
                    style=ButtonStyle.PRIMARY,
                )
            ],
        ]
    )


def global_manager_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Add Global Reply", callback_data="global:add", style=ButtonStyle.SUCCESS
                ),
                InlineKeyboardButton(
                    "View Global Replies", callback_data="global:list", style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "Clear Global Replies",
                    callback_data="global:confirm-clear",
                    style=ButtonStyle.DANGER,
                )
            ],
        ]
    )


def global_saved_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Add Another", callback_data="global:add", style=ButtonStyle.SUCCESS
                ),
                InlineKeyboardButton(
                    "View Global Replies", callback_data="global:list", style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "Back", callback_data="global:open", style=ButtonStyle.PRIMARY
                )
            ],
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
        f"Cooldown: {document.get('cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)} seconds\n"
        f"Rate limit: {document.get('rate_limit_per_minute', DEFAULT_RATE_LIMIT_PER_MINUTE) or 'unlimited'}/minute\n"
        f"Global replies: {'enabled' if document.get('global_replies_enabled', True) else 'disabled'}\n"
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


async def group_onboarding_content(
    client: Client,
    repository: GroupRepository,
    message: Message,
) -> tuple[str, InlineKeyboardMarkup | None]:
    me = await client.get_me()
    await repository.set_enabled(message.chat.id, True)

    try:
        bot_member = await client.get_chat_member(message.chat.id, me.id)
        is_admin = bot_member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}
    except RPCError as exc:
        LOGGER.warning("Could not inspect bot permissions in chat %s: %s", message.chat.id, exc)
        is_admin = False

    admin_line = (
        "Admin access: OK"
        if is_admin
        else "Admin access: missing. Promote me to administrator so I can reply reliably."
    )
    text = (
        "Thanks for adding me.\n\n"
        "Requirement check:\n"
        f"- {admin_line}\n"
        "- Send messages: OK, because this notice was delivered.\n"
        "- Privacy mode: please confirm it is disabled in BotFather so I can see normal group messages.\n\n"
        "Auto-reply is enabled by default. Add at least one local reply, or configure global defaults, "
        "and I will start responding according to this group's chance, cooldown, and rate-limit settings."
    )
    if not me.username:
        return text, None
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Open Auto Reply Manager",
                        url=f"https://t.me/{me.username}?start=configure_{message.chat.id}",
                        style=ButtonStyle.PRIMARY,
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Help",
                        callback_data="start:help",
                        style=ButtonStyle.PRIMARY,
                    )
                ],
            ]
        ),
    )


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
        keyboard = link_keyboard(links)
        if message.command and message.command[0].lower() == "help":
            await message.reply_text(HELP_TEXT, reply_markup=keyboard)
            return
        start_image = await repository.get_start_image()
        if start_image:
            try:
                await message.reply_photo(start_image, caption=START_TEXT, reply_markup=keyboard)
                return
            except RPCError:
                LOGGER.exception("Could not send configured start image")
        await message.reply_text(START_TEXT, reply_markup=keyboard)

    @app.on_callback_query(filters.regex(r"^start:help$"))
    async def start_help_callback(_: Client, query: CallbackQuery) -> None:
        if not query.message:
            return
        await query.message.reply_text(HELP_TEXT)
        await query.answer()

    @app.on_message(filters.private & filters.command(["autoreply", "reaction"]))
    async def private_manager_hint(_: Client, message: Message) -> None:
        await message.reply_text(
            "Send /autoreply in the group you want to configure, then open the private manager button."
        )

    @app.on_message(filters.private & filters.command("global_defaults"))
    async def global_defaults_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("Only the bot owner or sudoers can manage global default replies.")
            return
        text, keyboard = await global_manager_content(repository)
        await message.reply_text(text, reply_markup=keyboard)

    @app.on_message(filters.private & filters.command(["updates", "support", "owner_link"]))
    async def link_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("Only the bot owner or sudoers can configure start-menu links.")
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

    @app.on_message(filters.private & filters.command("start_img"))
    async def start_image_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("Only the bot owner or sudoers can configure the start image.")
            return
        argument = command_argument(message).lower()
        if argument == "off":
            await repository.set_start_image(None)
            await message.reply_text("Start-menu image removed.")
            return
        file_id = start_image_file_id(message)
        if not file_id:
            current = await repository.get_start_image()
            status = "configured" if current else "not configured"
            await message.reply_text(
                f"Start-menu image is {status}.\n"
                "Send a photo with /start_img as its caption, reply to a photo with /start_img, "
                "or use /start_img off."
            )
            return
        await repository.set_start_image(file_id)
        await message.reply_text("Start-menu image updated.")

    group_commands = filters.group & filters.command(COMMANDS)

    @app.on_message(filters.group & filters.new_chat_members)
    async def handle_added_to_group(client: Client, message: Message) -> None:
        me = await client.get_me()
        if not any(member.id == me.id for member in message.new_chat_members or []):
            return
        text, keyboard = await group_onboarding_content(client, repository, message)
        try:
            await message.reply_text(text, reply_markup=keyboard)
        except RPCError:
            LOGGER.exception("Could not send group onboarding notice in chat %s", message.chat.id)

    @app.on_message(group_commands)
    async def handle_command(client: Client, message: Message) -> None:
        if not await require_admin(client, message):
            return
        me = await client.get_me()
        launcher = await message.reply_text(
            "Configure this group privately.",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "Open Auto Reply Manager",
                        url=f"https://t.me/{me.username}?start=configure_{message.chat.id}",
                        style=ButtonStyle.PRIMARY,
                    )
                ]]
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
        elif action == "cooldown":
            document = await repository.get(chat_id)
            await repository.set_cooldown(
                chat_id,
                next_option(
                    document.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS),
                    COOLDOWN_OPTIONS,
                ),
            )
        elif action == "rate":
            document = await repository.get(chat_id)
            await repository.set_rate_limit(
                chat_id,
                next_option(
                    document.get("rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE),
                    RATE_LIMIT_OPTIONS,
                ),
            )
        elif action == "globals":
            await repository.toggle_global_replies(chat_id)
        elif action == "confirm-clear":
            await query.message.reply_text(
                "Clear all local replies for this group?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Yes, Clear",
                                callback_data=f"mgr:clear:{chat_id}",
                                style=ButtonStyle.DANGER,
                            ),
                            InlineKeyboardButton(
                                "Cancel",
                                callback_data=f"mgr:open:{chat_id}",
                                style=ButtonStyle.PRIMARY,
                            ),
                        ]
                    ]
                ),
            )
            await query.answer()
            return
        elif action == "clear":
            await repository.clear_responses(chat_id)
        elif action.startswith("exclude-"):
            try:
                index = int(action.removeprefix("exclude-"))
                response = (await repository.get_global_responses())[index - 1]
            except (ValueError, IndexError):
                await query.answer("Global reply not found.", show_alert=True)
                return
            await repository.toggle_global_exclusion(chat_id, response)
        elif action.startswith("delete-"):
            try:
                index = int(action.removeprefix("delete-"))
            except ValueError:
                await query.answer("Invalid reply number.", show_alert=True)
                return
            await repository.remove_response(chat_id, index)
        elif action.startswith("list"):
            try:
                page = int(action.split("-", 1)[1]) if "-" in action else 0
            except ValueError:
                page = 0
            document = await repository.get(chat_id)
            local_responses = document["responses"]
            global_responses = await repository.get_global_responses()
            combined = [("local", index, response) for index, response in enumerate(local_responses, 1)]
            combined += [("global", index, response) for index, response in enumerate(global_responses, 1)]
            page_count = max(1, (len(combined) + REPLIES_PER_PAGE - 1) // REPLIES_PER_PAGE)
            page = max(0, min(page, page_count - 1))
            page_items = combined[page * REPLIES_PER_PAGE : (page + 1) * REPLIES_PER_PAGE]
            excluded = document.get("excluded_global_responses", [])
            lines = []
            buttons = []
            for source, index, response in page_items:
                label = await display_response_label(client, response)
                if source == "local":
                    lines.append(f"L{index}. {label}")
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                f"Delete L{index}",
                                callback_data=f"mgr:delete-{index}:{chat_id}",
                                style=ButtonStyle.DANGER,
                            )
                        ]
                    )
                else:
                    is_excluded = response in excluded
                    lines.append(f"G{index}. {label}{' (excluded)' if is_excluded else ''}")
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                f"{'Include' if is_excluded else 'Exclude'} G{index}",
                                callback_data=f"mgr:exclude-{index}:{chat_id}",
                                style=ButtonStyle.SUCCESS if is_excluded else ButtonStyle.DANGER,
                            )
                        ]
                    )
            text = (
                f"Replies page {page + 1}/{page_count}:\n" + "\n".join(lines)
                if lines
                else "No replies configured."
            )
            navigation = []
            if page > 0:
                navigation.append(
                    InlineKeyboardButton("Previous", callback_data=f"mgr:list-{page - 1}:{chat_id}")
                )
            if page + 1 < page_count:
                navigation.append(
                    InlineKeyboardButton("Next", callback_data=f"mgr:list-{page + 1}:{chat_id}")
                )
            if navigation:
                buttons.append(navigation)
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Back", callback_data=f"mgr:open:{chat_id}", style=ButtonStyle.PRIMARY
                    )
                ]
            )
            await query.message.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return

        text, keyboard = await manager_content(client, repository, chat_id)
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer("Updated.")

    @app.on_callback_query(filters.regex(r"^global:"))
    async def global_callback(client: Client, query: CallbackQuery) -> None:
        if not query.message or not query_is_sudoer(settings, query):
            await query.answer("Only the bot owner or sudoers can manage global replies.", show_alert=True)
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
        elif action == "confirm-clear":
            await query.message.reply_text(
                "Clear all global replies?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Yes, Clear",
                                callback_data="global:clear",
                                style=ButtonStyle.DANGER,
                            ),
                            InlineKeyboardButton(
                                "Cancel", callback_data="global:open", style=ButtonStyle.PRIMARY
                            ),
                        ]
                    ]
                ),
            )
            await query.answer()
            return
        elif action.startswith("delete-"):
            try:
                index = int(action.removeprefix("delete-"))
            except ValueError:
                await query.answer("Invalid reply number.", show_alert=True)
                return
            await repository.remove_global_response(index)
        elif action.startswith("list"):
            try:
                page = int(action.split("-", 1)[1]) if "-" in action else 0
            except ValueError:
                page = 0
            responses = await repository.get_global_responses()
            page_count = max(1, (len(responses) + REPLIES_PER_PAGE - 1) // REPLIES_PER_PAGE)
            page = max(0, min(page, page_count - 1))
            page_items = list(enumerate(responses, 1))[
                page * REPLIES_PER_PAGE : (page + 1) * REPLIES_PER_PAGE
            ]
            labels = [
                (index, await display_response_label(client, response))
                for index, response in page_items
            ]
            text = (
                "No global default replies configured."
                if not responses
                else f"Global replies page {page + 1}/{page_count}:\n"
                + "\n".join(f"{index}. {label}" for index, label in labels)
            )
            buttons = [
                [
                    InlineKeyboardButton(
                        f"Delete {index}",
                        callback_data=f"global:delete-{index}",
                        style=ButtonStyle.DANGER,
                    )
                ]
                for index, _ in page_items
            ]
            navigation = []
            if page > 0:
                navigation.append(
                    InlineKeyboardButton("Previous", callback_data=f"global:list-{page - 1}")
                )
            if page + 1 < page_count:
                navigation.append(
                    InlineKeyboardButton("Next", callback_data=f"global:list-{page + 1}")
                )
            if navigation:
                buttons.append(navigation)
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Back", callback_data="global:open", style=ButtonStyle.PRIMARY
                    )
                ]
            )
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
                "start_img",
            ]
        ),
        group=1,
    )
    async def capture_private_message(client: Client, message: Message) -> None:
        if not message.from_user:
            return
        global_capture = (
            settings.is_sudoer(message.from_user.id)
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
        group_settings = await repository.get(message.chat.id)
        if not group_settings["enabled"] or not interaction_allowed(
            message.chat.id,
            group_settings.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS),
            group_settings.get("rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE),
        ):
            return
        reply_chance = group_settings.get("reply_chance", 100)
        response = (
            await repository.next_response(message.chat.id)
            if chance_succeeds(reply_chance)
            else None
        )
        if response:
            try:
                await retry_flood_wait(lambda: send_response(client, message, response))
            except (Forbidden, ChatAdminRequired):
                LOGGER.warning("Disabling inaccessible chat %s", message.chat.id)
                await repository.set_enabled(message.chat.id, False)
            except RPCError:
                LOGGER.exception("Could not reply in chat %s", message.chat.id)
            except (KeyError, TypeError, ValueError):
                LOGGER.exception("Invalid stored response in chat %s", message.chat.id)

        reaction_settings = await repository.reaction_settings(message.chat.id)
        reaction = choose_reaction(*reaction_settings) if reaction_settings else None
        if reaction:
            try:
                await retry_flood_wait(lambda: message.react(reaction))
            except ReactionInvalid:
                LOGGER.warning(
                    "Removing invalid reaction %r from chat %s",
                    reaction,
                    message.chat.id,
                )
                await repository.remove_reaction(message.chat.id, reaction)
            except (Forbidden, ChatAdminRequired):
                LOGGER.warning("Disabling inaccessible chat %s", message.chat.id)
                await repository.set_enabled(message.chat.id, False)
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
