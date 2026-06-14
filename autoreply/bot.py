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
    BotCommandScopeChat,
    BotCommandScopeDefault,
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
    DEFAULT_REPLY_CHANCE,
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
PUBLIC_BOT_COMMANDS = [
    BotCommand("start", "🚀 Open the bot"),
    BotCommand("help", "❓ Quick help"),
    BotCommand("autoreply", "⚙️ Manage a group"),
]
SUDOER_BOT_COMMANDS = [
    *PUBLIC_BOT_COMMANDS,
    BotCommand("updates", "📢 Set updates link"),
    BotCommand("support", "💬 Set support link"),
    BotCommand("owner_link", "👤 Set owner link"),
    BotCommand("global_defaults", "🌐 Manage global replies"),
    BotCommand("broadcast", "📣 Broadcast to groups"),
    BotCommand("start_img", "🖼 Set start image"),
]
START_TEXT = (
    "✨ Auto Reply\n\n"
    "Keep your groups lively with random replies and reactions.\n\n"
    "🚀 Add me as an admin\n"
    "🔓 Disable privacy mode in BotFather\n"
    "⚙️ Send /autoreply in your group"
)
HELP_TEXT = (
    "❓ Quick Help\n\n"
    "1️⃣ Add me as a group admin\n"
    "2️⃣ Disable privacy mode in BotFather\n"
    "3️⃣ Send /autoreply in the group\n"
    "4️⃣ Add replies in the private manager"
)
SUDOER_HELP_TEXT = (
    f"{HELP_TEXT}\n\n"
    "🌐 /global_defaults — global replies\n"
    "📣 /broadcast — message every group\n"
    "🖼 /start_img — start image\n"
    "🔗 /updates, /support, /owner_link — menu links"
)
SUDOER_PANEL_TEXT = (
    "🛡 Sudo Panel\n\n"
    "🌐 /global_defaults — global replies\n"
    "📣 /broadcast <text> — broadcast text\n"
    "📣 Reply with /broadcast — broadcast a message\n"
    "🖼 /start_img — start image\n"
    "🔗 /updates, /support, /owner_link — menu links"
)
MANAGER_DELETE_DELAY = 30
BROADCAST_BATCH_SIZE = 20
BROADCAST_BATCH_DELAY_SECONDS = 3
REPLIES_PER_PAGE = 10
REPLY_LABEL_LIMIT = 42
COOLDOWN_OPTIONS = [0, 5, 10, 15, 30, 60]
RATE_LIMIT_OPTIONS = [0, 5, 10, 20, 30]
_last_interaction: dict[int, float] = {}
_recent_interactions: dict[int, deque[float]] = defaultdict(deque)
T = TypeVar("T")


def command_argument(message: Message) -> str:
    text = message.text or ""
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


def broadcast_source(message: Message) -> object | None:
    argument = command_argument(message)
    if argument:
        return argument
    if message.reply_to_message:
        return {
            "kind": "message",
            "chat_id": message.reply_to_message.chat.id,
            "message_id": message.reply_to_message.id,
        }
    return None


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


def link_keyboard(
    links: dict[str, str],
    username: str | None = None,
    show_sudoer: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if username:
        rows.append(
            [
                InlineKeyboardButton(
                    "➕ Add to Group",
                    url=f"https://t.me/{username}?startgroup=true",
                    style=ButtonStyle.SUCCESS,
                )
            ]
        )
    buttons = [InlineKeyboardButton("❓ Help", callback_data="start:help")]
    if links.get("updates"):
        buttons.append(InlineKeyboardButton("📢 Updates", url=links["updates"]))
    if links.get("support"):
        buttons.append(InlineKeyboardButton("💬 Support", url=links["support"]))
    if links.get("owner_link"):
        buttons.append(InlineKeyboardButton("👤 Owner", url=links["owner_link"]))
    rows.extend(buttons[index : index + 2] for index in range(0, len(buttons), 2))
    if show_sudoer:
        rows.append(
            [
                InlineKeyboardButton(
                    "🛡 Sudo Panel",
                    callback_data="start:sudo",
                    style=ButtonStyle.DANGER,
                )
            ]
        )
    return InlineKeyboardMarkup(rows)


def sudoer_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🌐 Global Replies",
                    callback_data="global:open",
                    style=ButtonStyle.PRIMARY,
                ),
                InlineKeyboardButton(
                    "📣 Broadcast Help",
                    callback_data="start:broadcast-help",
                    style=ButtonStyle.PRIMARY,
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Back",
                    callback_data="start:back",
                    style=ButtonStyle.DANGER,
                )
            ],
        ]
    )


async def show_callback_menu(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if message.text:
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await message.reply_text(text, reply_markup=reply_markup)


async def register_bot_commands(app: Client, settings: Settings) -> None:
    await app.set_bot_commands(PUBLIC_BOT_COMMANDS, scope=BotCommandScopeDefault())
    for user_id in dict.fromkeys((settings.owner_id, *settings.sudoer_ids)):
        await app.set_bot_commands(SUDOER_BOT_COMMANDS, scope=BotCommandScopeChat(user_id))


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


def truncate_label(label: str, limit: int = REPLY_LABEL_LIMIT) -> str:
    label = " ".join(label.split())
    return label if len(label) <= limit else label[: limit - 3] + "..."


def callback_index_page(action: str, prefix: str) -> tuple[int, int]:
    values = action.removeprefix(prefix).split("-", 1)
    return int(values[0]), int(values[1]) if len(values) == 2 else 0


async def response_preview_text(client: Client, response: object, title: str) -> str:
    label = await display_response_label(client, response)
    return f"👁 {title}\n\n{label[:3900]}"


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


async def reply_list_content(
    client: Client,
    repository: GroupRepository,
    chat_id: int,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
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
        label = truncate_label(await display_response_label(client, response))
        if source == "local":
            lines.append(f"L{index}. {label}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"👁 L{index}", callback_data=f"mgr:preview-l-{index}-{page}:{chat_id}"
                    ),
                    InlineKeyboardButton(
                        f"🗑 L{index}",
                        callback_data=f"mgr:delete-{index}-{page}:{chat_id}",
                        style=ButtonStyle.DANGER,
                    ),
                ]
            )
        else:
            is_excluded = response in excluded
            lines.append(f"G{index}. {label}{' (excluded)' if is_excluded else ''}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"👁 G{index}", callback_data=f"mgr:preview-g-{index}-{page}:{chat_id}"
                    ),
                    InlineKeyboardButton(
                        f"{'✅ Include' if is_excluded else '🚫 Exclude'} G{index}",
                        callback_data=f"mgr:exclude-{index}-{page}:{chat_id}",
                        style=ButtonStyle.SUCCESS if is_excluded else ButtonStyle.DANGER,
                    ),
                ]
            )
    text = (
        f"📚 Replies • {page + 1}/{page_count}\n\n" + "\n".join(lines)
        if lines
        else "📭 No replies yet."
    )
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Prev",
                callback_data=f"mgr:list-{page - 1}:{chat_id}",
                style=ButtonStyle.DANGER,
            )
        )
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Next ➡️", callback_data=f"mgr:list-{page + 1}:{chat_id}"))
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [InlineKeyboardButton("⬅️ Manager", callback_data=f"mgr:open:{chat_id}", style=ButtonStyle.DANGER)]
    )
    return text[:4096], InlineKeyboardMarkup(buttons)


async def global_reply_list_content(
    client: Client,
    repository: GroupRepository,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    responses = await repository.get_global_responses()
    page_count = max(1, (len(responses) + REPLIES_PER_PAGE - 1) // REPLIES_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    page_items = list(enumerate(responses, 1))[
        page * REPLIES_PER_PAGE : (page + 1) * REPLIES_PER_PAGE
    ]
    labels = [
        (index, truncate_label(await display_response_label(client, response)))
        for index, response in page_items
    ]
    text = (
        "📭 No global replies yet."
        if not responses
        else f"🌐 Global Replies • {page + 1}/{page_count}\n\n"
        + "\n".join(f"{index}. {label}" for index, label in labels)
    )
    buttons = [
        [
            InlineKeyboardButton(f"👁 {index}", callback_data=f"global:preview-{index}-{page}"),
            InlineKeyboardButton(
                f"🗑 {index}",
                callback_data=f"global:delete-{index}-{page}",
                style=ButtonStyle.DANGER,
            ),
        ]
        for index, _ in page_items
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Prev",
                callback_data=f"global:list-{page - 1}",
                style=ButtonStyle.DANGER,
            )
        )
    if page + 1 < page_count:
        navigation.append(InlineKeyboardButton("Next ➡️", callback_data=f"global:list-{page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [InlineKeyboardButton("⬅️ Manager", callback_data="global:open", style=ButtonStyle.DANGER)]
    )
    return text[:4096], InlineKeyboardMarkup(buttons)


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


async def broadcast_response(
    client: Client,
    repository: GroupRepository,
    response: object,
) -> tuple[int, int]:
    sent = 0
    failed = 0
    group_ids = await repository.group_ids()
    for position, chat_id in enumerate(group_ids):
        try:
            if isinstance(response, str):
                await retry_flood_wait(
                    lambda chat_id=chat_id: client.send_message(chat_id=chat_id, text=response)
                )
            elif isinstance(response, dict) and response.get("kind") == "message":
                await retry_flood_wait(
                    lambda chat_id=chat_id: client.copy_message(
                        chat_id=chat_id,
                        from_chat_id=response["chat_id"],
                        message_id=response["message_id"],
                    )
                )
            else:
                raise ValueError("Unsupported broadcast response")
            sent += 1
        except (Forbidden, ChatAdminRequired):
            failed += 1
            await repository.set_enabled(chat_id, False)
        except RPCError:
            failed += 1
            LOGGER.exception("Could not broadcast to chat %s", chat_id)
        if (position + 1) % BROADCAST_BATCH_SIZE == 0 and position + 1 < len(group_ids):
            await asyncio.sleep(BROADCAST_BATCH_DELAY_SECONDS)
    return sent, failed


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
    enabled_label = "⏸ Disable" if document["enabled"] else "▶️ Enable"
    reactions_label = "🎭 Reactions: On" if document.get("reactions_enabled", True) else "🎭 Reactions: Off"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add Reply", callback_data=f"mgr:add:{chat_id}", style=ButtonStyle.SUCCESS
                ),
                InlineKeyboardButton(
                    "📚 Replies", callback_data=f"mgr:list:{chat_id}", style=ButtonStyle.PRIMARY
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
                    f"💬 Reply: {document.get('reply_chance', DEFAULT_REPLY_CHANCE)}%",
                    callback_data=f"mgr:reply-chance:{chat_id}",
                ),
                InlineKeyboardButton(
                    f"🎲 React: {document.get('reaction_chance', 25)}%",
                    callback_data=f"mgr:chance:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"⏱ {document.get('cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)}s",
                    callback_data=f"mgr:cooldown:{chat_id}",
                ),
                InlineKeyboardButton(
                    f"🚦 {document.get('rate_limit_per_minute', DEFAULT_RATE_LIMIT_PER_MINUTE) or '∞'}/min",
                    callback_data=f"mgr:rate:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🌐 Globals: On"
                    if document.get("global_replies_enabled", True)
                    else "🌐 Globals: Off",
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
                    "🗑 Clear Replies",
                    callback_data=f"mgr:confirm-clear:{chat_id}",
                    style=ButtonStyle.DANGER,
                ),
                InlineKeyboardButton("🔄 Refresh", callback_data=f"mgr:open:{chat_id}"),
            ],
        ]
    )


def saved_reply_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add Another",
                    callback_data=f"mgr:add:{chat_id}",
                    style=ButtonStyle.SUCCESS,
                ),
                InlineKeyboardButton(
                    "📚 Replies", callback_data=f"mgr:list:{chat_id}", style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Manager",
                    callback_data=f"mgr:open:{chat_id}",
                    style=ButtonStyle.DANGER,
                )
            ],
        ]
    )


def global_manager_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add Global", callback_data="global:add", style=ButtonStyle.SUCCESS
                ),
                InlineKeyboardButton(
                    "🌐 Replies", callback_data="global:list", style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑 Clear Globals",
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
                    "➕ Add Another", callback_data="global:add", style=ButtonStyle.SUCCESS
                ),
                InlineKeyboardButton(
                    "🌐 Replies", callback_data="global:list", style=ButtonStyle.PRIMARY
                ),
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Manager", callback_data="global:open", style=ButtonStyle.DANGER
                )
            ],
        ]
    )


async def global_manager_content(repository: GroupRepository) -> tuple[str, InlineKeyboardMarkup]:
    responses = await repository.get_global_responses()
    return (
        "🌐 Global Replies\n\n"
        f"📚 Saved: {len(responses)}\n"
        "Used by groups with globals enabled.",
        global_manager_keyboard(),
    )


async def manager_content(client: Client, repository: GroupRepository, chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    chat = await client.get_chat(chat_id)
    document = await repository.get(chat_id)
    local_count = len(document["responses"])
    global_count = len(await repository.get_global_responses())
    rate = document.get("rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE) or "∞"
    text = (
        f"⚙️ {chat.title or chat_id}\n\n"
        f"{'🟢 Active' if document['enabled'] else '🔴 Paused'}  •  "
        f"📚 {local_count} local + {global_count} global\n"
        f"💬 {document.get('reply_chance', DEFAULT_REPLY_CHANCE)}%  •  "
        f"🎲 {document.get('reaction_chance', 25)}%  •  "
        f"⏱ {document.get('cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)}s  •  🚦 {rate}/min"
    )
    return text, manager_keyboard(chat_id, document)


async def open_manager(client: Client, repository: GroupRepository, message: Message, chat_id: int) -> None:
    if not message.from_user or not await user_is_group_admin(client, chat_id, message.from_user.id):
        await message.reply_text("⛔ Group admins only.")
        return
    text, keyboard = await manager_content(client, repository, chat_id)
    await message.reply_text(text, reply_markup=keyboard)


async def require_admin(client: Client, message: Message) -> bool:
    try:
        allowed = await is_group_admin(client, message)
    except RPCError as exc:
        LOGGER.warning("Could not verify admin in chat %s: %s", message.chat.id, exc)
        await message.reply_text(
            "⚠️ Promote me to admin, then try again."
        )
        return False
    if not allowed:
        await message.reply_text("⛔ Group admins only.")
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

    actions = ["🔓 Disable privacy mode in BotFather", "⚙️ Add replies in the manager"]
    if not is_admin:
        actions.insert(0, "🛡 Promote me to group admin")
    text = "✨ Set Up Auto Reply\n\n" + "\n".join(actions)
    if not me.username:
        return text, None
    return (
        text,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⚙️ Open Manager",
                        url=f"https://t.me/{me.username}?start=configure_{message.chat.id}",
                        style=ButtonStyle.PRIMARY,
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❓ Help",
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
                await message.reply_text("⚠️ Invalid group link.")
                return
            await open_manager(client, repository, message, chat_id)
            return
        links = await repository.get_links()
        me = await client.get_me()
        keyboard = link_keyboard(links, me.username, is_sudoer(settings, message))
        if message.command and message.command[0].lower() == "help":
            help_text = SUDOER_HELP_TEXT if is_sudoer(settings, message) else HELP_TEXT
            await message.reply_text(help_text, reply_markup=keyboard)
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
        help_text = SUDOER_HELP_TEXT if query_is_sudoer(settings, query) else HELP_TEXT
        await query.message.reply_text(help_text)
        await query.answer()

    @app.on_callback_query(filters.regex(r"^start:(sudo|broadcast-help|back)$"))
    async def start_panel_callback(client: Client, query: CallbackQuery) -> None:
        if not query.message:
            return
        action = query.data.split(":", 1)[1]
        if action == "back":
            links = await repository.get_links()
            me = await client.get_me()
            await show_callback_menu(
                query.message,
                START_TEXT,
                link_keyboard(links, me.username, query_is_sudoer(settings, query)),
            )
            await query.answer()
            return
        if not query_is_sudoer(settings, query):
            await query.answer("⛔ Sudoers only.", show_alert=True)
            return
        if action == "broadcast-help":
            await show_callback_menu(
                query.message,
                "📣 Broadcast\n\n"
                "/broadcast <text>\n"
                "Or reply to any message with /broadcast.",
                InlineKeyboardMarkup(
                    [[
                        InlineKeyboardButton(
                            "⬅️ Sudo Panel",
                            callback_data="start:sudo",
                            style=ButtonStyle.DANGER,
                        )
                    ]]
                ),
            )
        else:
            await show_callback_menu(query.message, SUDOER_PANEL_TEXT, sudoer_panel_keyboard())
        await query.answer()

    @app.on_message(filters.private & filters.command(["autoreply", "reaction"]))
    async def private_manager_hint(_: Client, message: Message) -> None:
        await message.reply_text("⚙️ Send /autoreply in your group.")

    @app.on_message(filters.private & filters.command("global_defaults"))
    async def global_defaults_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("⛔ Sudoers only.")
            return
        text, keyboard = await global_manager_content(repository)
        await message.reply_text(text, reply_markup=keyboard)

    @app.on_message(filters.private & filters.command("broadcast"))
    async def broadcast_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("⛔ Sudoers only.")
            return
        response = broadcast_source(message)
        if response is None:
            await message.reply_text(
                "📣 Usage:\n/broadcast <text>\n\nOr reply to any message with /broadcast."
            )
            return
        await repository.set_pending_broadcast(message.from_user.id, response)
        group_count = len(await repository.group_ids())
        await message.reply_text(
            f"📣 Send this to {group_count} known groups?\n\n"
            f"⏱ {BROADCAST_BATCH_DELAY_SECONDS}s pause per {BROADCAST_BATCH_SIZE} groups",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📣 Broadcast",
                            callback_data="broadcast:send",
                            style=ButtonStyle.SUCCESS,
                        ),
                        InlineKeyboardButton("✖️ Cancel", callback_data="broadcast:cancel"),
                    ]
                ]
            ),
        )

    @app.on_message(filters.private & filters.command(["updates", "support", "owner_link"]))
    async def link_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("⛔ Sudoers only.")
            return

        name = message.command[0].lower()
        value = command_argument(message)
        labels = {"updates": "Updates channel", "support": "Support group", "owner_link": "Owner"}
        if not value:
            current = (await repository.get_links()).get(name, "not configured")
            await message.reply_text(f"🔗 {labels[name]}: {current}\n/{name} <url> or /{name} off")
        elif value.lower() == "off":
            await repository.set_link(name, None)
            await message.reply_text(f"✅ {labels[name]} removed.")
        elif not valid_link(value):
            await message.reply_text("⚠️ Use an https://, http://, or tg:// link.")
        else:
            await repository.set_link(name, value)
            await message.reply_text(f"✅ {labels[name]} updated.")

    @app.on_message(filters.private & filters.command("start_img"))
    async def start_image_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("⛔ Sudoers only.")
            return
        argument = command_argument(message).lower()
        if argument == "off":
            await repository.set_start_image(None)
            await message.reply_text("✅ Start image removed.")
            return
        file_id = start_image_file_id(message)
        if not file_id:
            current = await repository.get_start_image()
            status = "configured" if current else "not configured"
            await message.reply_text(
                f"🖼 Start image: {status}\n"
                "Send/reply to a photo with /start_img, or use /start_img off."
            )
            return
        await repository.set_start_image(file_id)
        await message.reply_text("✅ Start image updated.")

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
            "⚙️ Manage this group privately.",
            reply_markup=InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton(
                        "⚙️ Open Manager",
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
            await query.answer("⚠️ Invalid action.", show_alert=True)
            return
        if not await user_is_group_admin(client, chat_id, query.from_user.id):
            await query.answer("⛔ Group admins only.", show_alert=True)
            return

        if action == "add":
            await repository.set_capture_group(query.from_user.id, chat_id)
            await query.message.reply_text(
                "➕ Send the reply to save.\n\n/cancel to stop."
            )
            await query.answer("📥 Waiting for reply…")
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
            chance = (document.get("reply_chance", DEFAULT_REPLY_CHANCE) + 25) % 125
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
                "🗑 Clear every local reply?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🗑 Clear",
                                callback_data=f"mgr:clear:{chat_id}",
                                style=ButtonStyle.DANGER,
                            ),
                            InlineKeyboardButton(
                                "✖️ Cancel",
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
        elif action.startswith("preview-"):
            try:
                source, raw_index, raw_page = action.removeprefix("preview-").split("-", 2)
                index = int(raw_index)
                page = int(raw_page)
                responses = (
                    (await repository.get(chat_id))["responses"]
                    if source == "l"
                    else await repository.get_global_responses()
                )
                response = responses[index - 1]
            except (ValueError, IndexError):
                await query.answer("⚠️ Reply not found.", show_alert=True)
                return
            text = await response_preview_text(client, response, f"{source.upper()}{index}")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Replies",
                            callback_data=f"mgr:list-{page}:{chat_id}",
                            style=ButtonStyle.DANGER,
                        ),
                        InlineKeyboardButton(
                            "🗑 Delete" if source == "l" else "🚫 Exclude",
                            callback_data=(
                                f"mgr:delete-{index}-{page}:{chat_id}"
                                if source == "l"
                                else f"mgr:exclude-{index}-{page}:{chat_id}"
                            ),
                            style=ButtonStyle.DANGER,
                        ),
                    ]
                ]
            )
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer()
            return
        elif action.startswith("exclude-"):
            try:
                index, page = callback_index_page(action, "exclude-")
                response = (await repository.get_global_responses())[index - 1]
            except (ValueError, IndexError):
                await query.answer("⚠️ Reply not found.", show_alert=True)
                return
            await repository.toggle_global_exclusion(chat_id, response)
            text, keyboard = await reply_list_content(client, repository, chat_id, page)
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer("✅ Updated")
            return
        elif action.startswith("delete-"):
            try:
                index, page = callback_index_page(action, "delete-")
            except ValueError:
                await query.answer("⚠️ Invalid reply.", show_alert=True)
                return
            await repository.remove_response(chat_id, index)
            text, keyboard = await reply_list_content(client, repository, chat_id, page)
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer("🗑 Deleted")
            return
        elif action.startswith("list"):
            try:
                page = int(action.split("-", 1)[1]) if "-" in action else 0
            except ValueError:
                page = 0
            text, keyboard = await reply_list_content(client, repository, chat_id, page)
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer()
            return

        text, keyboard = await manager_content(client, repository, chat_id)
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer("✅ Updated")

    @app.on_callback_query(filters.regex(r"^global:"))
    async def global_callback(client: Client, query: CallbackQuery) -> None:
        if not query.message or not query_is_sudoer(settings, query):
            await query.answer("⛔ Sudoers only.", show_alert=True)
            return
        action = query.data.split(":", 1)[1]
        if action == "add":
            await repository.set_global_capture(query.from_user.id)
            await query.message.reply_text(
                "🌐 Send the global reply to save.\n\n/cancel to stop."
            )
            await query.answer("📥 Waiting for reply…")
            return
        if action == "clear":
            await repository.clear_global_responses()
        elif action == "confirm-clear":
            await query.message.reply_text(
                "🗑 Clear every global reply?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🗑 Clear",
                                callback_data="global:clear",
                                style=ButtonStyle.DANGER,
                            ),
                            InlineKeyboardButton(
                                "✖️ Cancel", callback_data="global:open", style=ButtonStyle.PRIMARY
                            ),
                        ]
                    ]
                ),
            )
            await query.answer()
            return
        elif action.startswith("preview-"):
            try:
                index, page = callback_index_page(action, "preview-")
                response = (await repository.get_global_responses())[index - 1]
            except (ValueError, IndexError):
                await query.answer("⚠️ Reply not found.", show_alert=True)
                return
            text = await response_preview_text(client, response, f"Global Reply {index}")
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⬅️ Replies",
                            callback_data=f"global:list-{page}",
                            style=ButtonStyle.DANGER,
                        ),
                        InlineKeyboardButton(
                            "🗑 Delete",
                            callback_data=f"global:delete-{index}-{page}",
                            style=ButtonStyle.DANGER,
                        ),
                    ]
                ]
            )
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer()
            return
        elif action.startswith("delete-"):
            try:
                index, page = callback_index_page(action, "delete-")
            except ValueError:
                await query.answer("⚠️ Invalid reply.", show_alert=True)
                return
            await repository.remove_global_response(index)
            text, keyboard = await global_reply_list_content(client, repository, page)
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer("🗑 Deleted")
            return
        elif action.startswith("list"):
            try:
                page = int(action.split("-", 1)[1]) if "-" in action else 0
            except ValueError:
                page = 0
            text, keyboard = await global_reply_list_content(client, repository, page)
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer()
            return
        text, keyboard = await global_manager_content(repository)
        await query.message.edit_text(text, reply_markup=keyboard)
        await query.answer("✅ Updated")

    @app.on_callback_query(filters.regex(r"^broadcast:"))
    async def broadcast_callback(client: Client, query: CallbackQuery) -> None:
        if not query.message or not query_is_sudoer(settings, query):
            await query.answer("⛔ Sudoers only.", show_alert=True)
            return
        action = query.data.split(":", 1)[1]
        if action == "cancel":
            await repository.clear_capture_group(query.from_user.id)
            await query.message.edit_text("✖️ Broadcast cancelled.")
            await query.answer()
            return
        response = await repository.get_pending_broadcast(query.from_user.id)
        if action != "send" or not response:
            await query.answer("⚠️ Broadcast expired.", show_alert=True)
            return
        await query.answer("📣 Broadcasting…")
        await query.message.edit_text(
            f"📣 Broadcasting…\n\n⏱ {BROADCAST_BATCH_DELAY_SECONDS}s pause per "
            f"{BROADCAST_BATCH_SIZE} groups"
        )
        sent, failed = await broadcast_response(client, repository, response)
        await repository.clear_capture_group(query.from_user.id)
        await query.message.edit_text(
            f"✅ Broadcast complete\n\n📨 Sent: {sent}\n⚠️ Failed: {failed}"
        )

    @app.on_message(filters.private & filters.command("cancel"))
    async def cancel_capture(_: Client, message: Message) -> None:
        if message.from_user:
            await repository.clear_capture_group(message.from_user.id)
        await message.reply_text("✖️ Cancelled.")

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
                "broadcast",
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
            await message.reply_text("⛔ Group admins only.")
            return
        source = message
        if settings.storage_chat_id:
            try:
                source = await client.copy_message(settings.storage_chat_id, message.chat.id, message.id)
            except RPCError:
                LOGGER.exception("Could not copy captured reply to storage chat")
                await message.reply_text(
                    "⚠️ Could not save it. Check STORAGE_CHAT_ID and my permissions."
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
            "added": "✅ Reply saved.",
            "duplicate": "⚠️ Already saved.",
            "full": f"⚠️ Reply limit reached ({MAX_RESPONSES}).",
        }
        if global_capture:
            replies["added"] = "✅ Global reply saved and live."
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
        reply_chance = group_settings.get("reply_chance", DEFAULT_REPLY_CHANCE)
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
        await register_bot_commands(app, settings)
        LOGGER.info("Interaction bot started and command menu registered")
        await idle()
    finally:
        await app.stop()
        await repository.close()


def run() -> None:
    import asyncio

    asyncio.run(start())
