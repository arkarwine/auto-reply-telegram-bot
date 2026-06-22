import asyncio
import logging
import random
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from io import BytesIO
from time import monotonic
from typing import TypeVar

from pyrogram import Client, filters, idle
from pyrogram.enums import ButtonStyle, ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import (
    ChatAdminRequired,
    FloodWait,
    Forbidden,
    ReactionInvalid,
    RPCError,
)
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
    DEFAULT_REACTION_CHANCE,
    DEFAULT_REPLY_CHANCE,
    GroupRepository,
    split_keywords,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)
COMMANDS = [
    "autoreply",
    "reaction",
    "delete_replies",
    "delete_all_replies",
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
    BotCommand("sudos", "🛡 Open sudo panel"),
    BotCommand("globals", "🌐 Manage global replies"),
    BotCommand("broadcast", "📣 Broadcast to groups"),
    BotCommand("stats", "📊 Bot statistics"),
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
    "🛡 /sudos — open sudo tools, stats, globals, broadcasts, and menu links."
)
SUDOER_PANEL_TEXT = "🛡 Sudo Panel\n\nChoose the area you want to manage."
SUDOER_COMMANDS_TEXT = (
    "⌘ Sudo Commands\n\n"
    "🌐 /globals — global replies\n"
    "📣 Reply with /broadcast — forward to groups\n"
    "📣 /broadcast -copy — send without forward header\n"
    "👤 /broadcast -user — also send to users\n"
    "📊 /stats — usage statistics\n"
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
    tokens = command_argument(message).split()
    if not message.reply_to_message or any(
        token not in {"-user", "-copy"} for token in tokens
    ):
        return None
    return {
        "kind": "message",
        "chat_id": message.reply_to_message.chat.id,
        "message_id": message.reply_to_message.id,
        "include_users": "-user" in tokens,
        "copy": "-copy" in tokens,
    }
    return None


def choose_reaction(chance: int, reactions: list[str]) -> str | None:
    reactions = [
        reaction.strip() for reaction in reactions if reaction and reaction.strip()
    ]
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


def next_local_option(document: dict, name: str, options: list) -> object | None:
    current = document[name]
    if name not in document.get("config_overrides", []):
        return options[0]
    if current == options[-1]:
        return None
    try:
        return options[(options.index(current) + 1) % len(options)]
    except ValueError:
        return options[0]


def interaction_allowed(
    chat_id: int, cooldown: int, per_minute: int, now: float | None = None
) -> bool:
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


async def validate_reaction(message: Message, reaction: str) -> bool:
    try:
        await retry_flood_wait(lambda: message.react(reaction))
        await retry_flood_wait(lambda: message.react())
    except ReactionInvalid:
        return False
    except RPCError:
        LOGGER.exception("Could not validate reaction %r", reaction)
        return False
    return True


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
                    callback_data="start:globals",
                ),
                InlineKeyboardButton(
                    "📊 Stats",
                    callback_data="start:stats",
                ),
            ],
            [
                InlineKeyboardButton("⌘ Commands", callback_data="start:commands"),
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


def sudoer_commands_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Sudo Panel", callback_data="start:sudo", style=ButtonStyle.DANGER
                )
            ]
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
        await app.set_bot_commands(
            SUDOER_BOT_COMMANDS, scope=BotCommandScopeChat(user_id)
        )


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


def keyword_entry_label(entry: dict) -> str:
    return ", ".join(entry.get("keywords", [])) or "keyword"


def keyword_response(entry: dict) -> object:
    return entry.get("response")


def truncate_label(label: str, limit: int = REPLY_LABEL_LIMIT) -> str:
    label = " ".join(label.split())
    return label if len(label) <= limit else label[: limit - 3] + "..."


def callback_index_page(action: str, prefix: str) -> tuple[int, int]:
    values = action.removeprefix(prefix).split("-", 1)
    return int(values[0]), int(values[1]) if len(values) == 2 else 0


async def response_preview_text(client: Client, response: object, title: str) -> str:
    label = await display_response_label(client, response)
    return f"👁 {title}\n\n{label[:3900]}"


def preview_keyboard(
    back_data: str, action_text: str, action_data: str
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Back", callback_data=back_data, style=ButtonStyle.DANGER
                ),
                InlineKeyboardButton(action_text, callback_data=action_data),
            ]
        ]
    )


async def show_response_preview(
    client: Client,
    query: CallbackQuery,
    response: object,
    title: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if isinstance(response, dict) and response.get("kind") == "message":
        try:
            await client.copy_message(
                chat_id=query.message.chat.id,
                from_chat_id=response["chat_id"],
                message_id=response["message_id"],
            )
            await query.message.reply_text(f"👁 {title}", reply_markup=reply_markup)
            return
        except (KeyError, RPCError):
            LOGGER.exception("Could not copy preview message")
    await query.message.edit_text(
        await response_preview_text(client, response, title),
        reply_markup=reply_markup,
    )


async def display_response_label(client: Client, response: object) -> str:
    if (
        isinstance(response, dict)
        and response.get("kind") == "message"
        and response.get("label") == "text"
        and not response.get("has_preview")
    ):
        try:
            source = await client.get_messages(
                response["chat_id"], response["message_id"]
            )
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
    keyword_mode = document.get("reply_mode") == "keyword"
    local_responses = (
        document.get("keyword_responses", []) if keyword_mode else document["responses"]
    )
    global_responses = (
        (
            await repository.get_global_keyword_responses()
            if keyword_mode
            else await repository.get_global_responses()
        )
        if document.get("global_replies_enabled", True)
        else []
    )
    combined = [
        ("keyword" if keyword_mode else "local", index, response)
        for index, response in enumerate(local_responses, 1)
    ]
    combined += [
        ("global-keyword" if keyword_mode else "global", index, response)
        for index, response in enumerate(global_responses, 1)
    ]
    page_count = max(1, (len(combined) + REPLIES_PER_PAGE - 1) // REPLIES_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    page_items = combined[page * REPLIES_PER_PAGE : (page + 1) * REPLIES_PER_PAGE]
    excluded = document.get("excluded_global_responses", [])
    lines = []
    buttons = []
    for source, index, response in page_items:
        stored_response = (
            keyword_response(response) if source.endswith("keyword") else response
        )
        label = truncate_label(await display_response_label(client, stored_response))
        if source in {"local", "keyword"}:
            prefix = "K" if source == "keyword" else "L"
            if source == "keyword":
                label = f"{keyword_entry_label(response)} -> {label}"
            lines.append(f"{prefix}{index}. {label}")
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"👁 {prefix}{index}",
                        callback_data=f"mgr:preview-{'k' if source == 'keyword' else 'l'}-{index}-{page}:{chat_id}",
                    ),
                    InlineKeyboardButton(
                        f"🗑 {prefix}{index}",
                        callback_data=(
                            f"mgr:delete-keyword-{index}-{page}:{chat_id}"
                            if source == "keyword"
                            else f"mgr:delete-{index}-{page}:{chat_id}"
                        ),
                        style=ButtonStyle.DANGER,
                    ),
                ]
            )
        else:
            if source == "global-keyword":
                label = f"{keyword_entry_label(response)} -> {label}"
            is_excluded = response in excluded
            lines.append(f"G{index}. {label}{' (excluded)' if is_excluded else ''}")
            row = [
                InlineKeyboardButton(
                    f"👁 G{index}",
                    callback_data=(
                        f"mgr:preview-gk-{index}-{page}:{chat_id}"
                        if source == "global-keyword"
                        else f"mgr:preview-g-{index}-{page}:{chat_id}"
                    ),
                )
            ]
            if source == "global":
                row.append(
                    InlineKeyboardButton(
                        f"{'✅ Include' if is_excluded else '🚫 Exclude'} G{index}",
                        callback_data=f"mgr:exclude-{index}-{page}:{chat_id}",
                        style=ButtonStyle.SUCCESS
                        if is_excluded
                        else ButtonStyle.DANGER,
                    )
                )
            buttons.append(row)
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
        navigation.append(
            InlineKeyboardButton(
                "Next ➡️", callback_data=f"mgr:list-{page + 1}:{chat_id}"
            )
        )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Manager",
                callback_data=f"mgr:open:{chat_id}",
                style=ButtonStyle.DANGER,
            )
        ]
    )
    return text[:4096], InlineKeyboardMarkup(buttons)


async def global_reply_list_content(
    client: Client,
    repository: GroupRepository,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    keyword_mode = (await repository.get_global_config()).get("reply_mode") == "keyword"
    responses = (
        await repository.get_global_keyword_responses()
        if keyword_mode
        else await repository.get_global_responses()
    )
    page_count = max(1, (len(responses) + REPLIES_PER_PAGE - 1) // REPLIES_PER_PAGE)
    page = max(0, min(page, page_count - 1))
    page_items = list(enumerate(responses, 1))[
        page * REPLIES_PER_PAGE : (page + 1) * REPLIES_PER_PAGE
    ]
    labels = []
    for index, response in page_items:
        stored_response = keyword_response(response) if keyword_mode else response
        label = truncate_label(await display_response_label(client, stored_response))
        if keyword_mode:
            label = f"{keyword_entry_label(response)} -> {label}"
        labels.append((index, label))
    text = (
        "📭 No global replies yet."
        if not responses
        else f"🌐 Global {'Keyword ' if keyword_mode else ''}Replies • {page + 1}/{page_count}\n\n"
        + "\n".join(f"{index}. {label}" for index, label in labels)
    )
    buttons = [
        [
            InlineKeyboardButton(
                f"👁 {index}", callback_data=f"global:preview-{index}-{page}"
            ),
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
        navigation.append(
            InlineKeyboardButton("Next ➡️", callback_data=f"global:list-{page + 1}")
        )
    if navigation:
        buttons.append(navigation)
    buttons.append(
        [
            InlineKeyboardButton(
                "⬅️ Manager", callback_data="global:open", style=ButtonStyle.DANGER
            )
        ]
    )
    return text[:4096], InlineKeyboardMarkup(buttons)


async def reaction_list_content(
    repository: GroupRepository,
    chat_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    document = await repository.get(chat_id)
    keyword_mode = document.get("reply_mode") == "keyword"
    global_reactions_enabled = document.get("global_reactions_enabled", True)
    if keyword_mode:
        local_entries = document.get("keyword_reactions", [])
        global_entries = (
            await repository.get_global_keyword_reactions()
            if global_reactions_enabled
            else []
        )
        lines = [
            f"{index}. {keyword_entry_label(entry)} -> {entry.get('reaction', '')}"
            for index, entry in enumerate(local_entries, 1)
        ]
        offset = len(lines)
        lines.extend(
            f"{index}. 🌐 {keyword_entry_label(entry)} -> {entry.get('reaction', '')}"
            for index, entry in enumerate(global_entries, offset + 1)
        )
    else:
        local_entries = (
            list(document.get("reactions", []))
            if "reactions" in document.get("config_overrides", [])
            else []
        )
        global_entries = (
            list((await repository.get_global_config()).get("reactions", []))
            if global_reactions_enabled
            else []
        )
        lines = [
            f"{index}. {reaction}" for index, reaction in enumerate(local_entries, 1)
        ]
        offset = len(lines)
        lines.extend(
            f"{index}. 🌐 {reaction}"
            for index, reaction in enumerate(global_entries, offset + 1)
        )
    text = "🎭 Reactions\n\n" + "\n".join(lines) if lines else "📭 No reactions yet."
    return (
        text[:4096],
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Manager",
                        callback_data=f"mgr:open:{chat_id}",
                        style=ButtonStyle.DANGER,
                    )
                ]
            ]
        ),
    )


async def global_reaction_list_content(
    repository: GroupRepository,
) -> tuple[str, InlineKeyboardMarkup]:
    keyword_mode = (await repository.get_global_config()).get("reply_mode") == "keyword"
    reactions = (
        await repository.get_global_keyword_reactions()
        if keyword_mode
        else (await repository.get_global_config()).get("reactions", [])
    )
    if keyword_mode:
        lines = [
            f"{index}. {keyword_entry_label(entry)} -> {entry.get('reaction', '')}"
            for index, entry in enumerate(reactions, 1)
        ]
    else:
        lines = [f"{index}. {reaction}" for index, reaction in enumerate(reactions, 1)]
    text = "🎭 Reactions\n\n" + "\n".join(lines) if lines else "📭 No reactions yet."
    return (
        text[:4096],
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Manager",
                        callback_data="global:open",
                        style=ButtonStyle.DANGER,
                    )
                ]
            ]
        ),
    )


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
    progress: Callable[[int, int, int, int], Awaitable[None]] | None = None,
) -> tuple[int, list[str]]:
    if not isinstance(response, dict) or response.get("kind") != "message":
        raise ValueError("Unsupported broadcast response")
    sent = 0
    errors = []
    targets = [("group", chat_id) for chat_id in await repository.group_ids()]
    if response.get("include_users"):
        targets.extend(("user", user_id) for user_id in await repository.user_ids())
    for position, (target_type, chat_id) in enumerate(targets):
        try:
            if response.get("copy"):
                await retry_flood_wait(
                    lambda chat_id=chat_id: client.copy_message(
                        chat_id=chat_id,
                        from_chat_id=response["chat_id"],
                        message_id=response["message_id"],
                    )
                )
            else:
                await retry_flood_wait(
                    lambda chat_id=chat_id: client.forward_messages(
                        chat_id=chat_id,
                        from_chat_id=response["chat_id"],
                        message_ids=response["message_id"],
                    )
                )
            sent += 1
        except (Forbidden, ChatAdminRequired) as exc:
            errors.append(f"{target_type}:{chat_id}: {type(exc).__name__}: {exc}")
            if target_type == "group":
                await repository.set_enabled(chat_id, False)
        except RPCError as exc:
            errors.append(f"{target_type}:{chat_id}: {type(exc).__name__}: {exc}")
            LOGGER.exception("Could not broadcast to chat %s", chat_id)
        processed = position + 1
        if progress and (
            processed % BROADCAST_BATCH_SIZE == 0 or processed == len(targets)
        ):
            await progress(processed, len(targets), sent, len(errors))
        if (position + 1) % BROADCAST_BATCH_SIZE == 0 and position + 1 < len(targets):
            await asyncio.sleep(BROADCAST_BATCH_DELAY_SECONDS)
    return sent, errors


def broadcast_progress_text(processed: int, total: int, sent: int, failed: int) -> str:
    percent = round(processed / total * 100) if total else 100
    return (
        f"📣 Broadcasting… {percent}%\n\n"
        f"📬 Targets: {processed}/{total}\n"
        f"✅ Sent: {sent}\n"
        f"⚠️ Failed: {failed}"
    )


async def send_broadcast_errors(message: Message, errors: list[str]) -> None:
    if not errors:
        return
    report = BytesIO(("\n".join(errors) + "\n").encode())
    report.name = "broadcast_errors.txt"
    await message.reply_document(report, caption=f"⚠️ {len(errors)} broadcast failures")


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
    overrides = set(document.get("config_overrides", []))
    keyword_mode = document.get("reply_mode") == "keyword"
    mode_label = "🎯 Mode: Keyword" if keyword_mode else "🎲 Mode: Random"
    if "reply_mode" not in overrides:
        mode_label = "🌐 Mode: Global"
    enabled_label = "⏸ Enabled" if document["enabled"] else "▶️ Disabled"
    if "enabled" not in overrides:
        enabled_label = "🌐 Status: Global"
    setting = lambda name, label: (
        label if name in overrides else f"{label.split(':', 1)[0]}: Global"
    )
    rows = [
        [
            InlineKeyboardButton(
                mode_label,
                callback_data=f"mgr:mode:{chat_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "➕ Add Replies",
                callback_data=f"mgr:add:{chat_id}",
                style=ButtonStyle.SUCCESS,
            ),
            InlineKeyboardButton(
                "📚 Replies",
                callback_data=f"mgr:list:{chat_id}",
                style=ButtonStyle.PRIMARY,
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ Add Reactions",
                callback_data=f"mgr:add-reaction:{chat_id}",
                style=ButtonStyle.SUCCESS,
            ),
            InlineKeyboardButton(
                "🎭 Reactions",
                callback_data=f"mgr:reaction-list:{chat_id}",
                style=ButtonStyle.PRIMARY,
            ),
        ],
        [
            InlineKeyboardButton(
                "🌐 Global Replies: On"
                if document.get("global_replies_enabled", True)
                else "🌐 Global Replies: Off",
                callback_data=f"mgr:globals:{chat_id}",
            ),
            InlineKeyboardButton(
                "🌐 Global Reactions: On"
                if document.get("global_reactions_enabled", True)
                else "🌐 Global Reactions: Off",
                callback_data=f"mgr:reactions:{chat_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                enabled_label,
                callback_data=f"mgr:toggle:{chat_id}",
                style=(
                    ButtonStyle.DANGER
                    if "enabled" in overrides and document["enabled"]
                    else ButtonStyle.SUCCESS
                    if "enabled" in overrides
                    else ButtonStyle.DEFAULT
                ),
            ),
        ],
    ]
    if keyword_mode:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        "🗑 Clear Replies",
                        callback_data=f"mgr:confirm-clear:{chat_id}",
                        style=ButtonStyle.DANGER,
                    ),
                    InlineKeyboardButton(
                        "🎭 Clear Reactions",
                        callback_data=f"mgr:confirm-clear-reactions:{chat_id}",
                        style=ButtonStyle.DANGER,
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data=f"mgr:open:{chat_id}",
                        style=ButtonStyle.SUCCESS,
                    )
                ],
            ]
        )
        return InlineKeyboardMarkup(rows)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    setting(
                        "reply_chance",
                        f"Reply: {document.get('reply_chance', DEFAULT_REPLY_CHANCE)}%",
                    ),
                    callback_data=f"mgr:reply-chance:{chat_id}",
                ),
                InlineKeyboardButton(
                    setting(
                        "reaction_chance",
                        f"React: {document.get('reaction_chance', DEFAULT_REACTION_CHANCE)}%",
                    ),
                    callback_data=f"mgr:chance:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    setting(
                        "cooldown_seconds",
                        f"Cooldown: {document.get('cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)}s",
                    ),
                    callback_data=f"mgr:cooldown:{chat_id}",
                ),
                InlineKeyboardButton(
                    setting(
                        "rate_limit_per_minute",
                        f"Rate: {document.get('rate_limit_per_minute', DEFAULT_RATE_LIMIT_PER_MINUTE) or '∞'}/min",
                    ),
                    callback_data=f"mgr:rate:{chat_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🗑 Clear Replies",
                    callback_data=f"mgr:confirm-clear:{chat_id}",
                    style=ButtonStyle.DANGER,
                ),
                InlineKeyboardButton(
                    "🎭 Clear Reactions",
                    callback_data=f"mgr:confirm-clear-reactions:{chat_id}",
                    style=ButtonStyle.DANGER,
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"mgr:open:{chat_id}",
                    style=ButtonStyle.SUCCESS,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


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
                    "📚 Replies",
                    callback_data=f"mgr:list:{chat_id}",
                    style=ButtonStyle.PRIMARY,
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


def global_manager_keyboard(config: dict | None = None) -> InlineKeyboardMarkup:
    config = config or {
        "enabled": True,
        "reply_chance": DEFAULT_REPLY_CHANCE,
        "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
        "rate_limit_per_minute": DEFAULT_RATE_LIMIT_PER_MINUTE,
        "reactions_enabled": True,
        "reaction_chance": DEFAULT_REACTION_CHANCE,
    }
    keyword_mode = config.get("reply_mode") == "keyword"
    rows = [
        [
            InlineKeyboardButton(
                "🎯 Mode: Keyword" if keyword_mode else "🎲 Mode: Random",
                callback_data="global:mode",
            )
        ],
        [
            InlineKeyboardButton(
                "➕ Add Replies", callback_data="global:add", style=ButtonStyle.SUCCESS
            ),
            InlineKeyboardButton(
                "🌐 Replies", callback_data="global:list", style=ButtonStyle.PRIMARY
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ Add Reactions",
                callback_data="global:add-reaction",
                style=ButtonStyle.SUCCESS,
            ),
            InlineKeyboardButton(
                "🎭 Reactions",
                callback_data="global:reaction-list",
                style=ButtonStyle.PRIMARY,
            ),
        ],
        [
            InlineKeyboardButton(
                "⏸ New Groups: Off" if config["enabled"] else "▶️ New Groups: On",
                callback_data="global:toggle",
            ),
        ],
    ]
    if not keyword_mode:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        "🎭 Reactions: On"
                        if config.get("reactions_enabled", True)
                        else "🎭 Reactions: Off",
                        callback_data="global:reactions",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        f"💬 Reply: {config.get('reply_chance', DEFAULT_REPLY_CHANCE)}%",
                        callback_data="global:reply-chance",
                    ),
                    InlineKeyboardButton(
                        f"🎲 React: {config.get('reaction_chance', DEFAULT_REACTION_CHANCE)}%",
                        callback_data="global:chance",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        f"⏱ {config.get('cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)}s",
                        callback_data="global:cooldown",
                    ),
                    InlineKeyboardButton(
                        f"🚦 {config.get('rate_limit_per_minute', DEFAULT_RATE_LIMIT_PER_MINUTE) or '∞'}/min",
                        callback_data="global:rate",
                    ),
                ],
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "🗑 Clear Replies",
                    callback_data="global:confirm-clear",
                    style=ButtonStyle.DANGER,
                ),
                InlineKeyboardButton(
                    "🎭 Clear Reactions",
                    callback_data="global:confirm-clear-reactions",
                    style=ButtonStyle.DANGER,
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data="global:open",
                    style=ButtonStyle.SUCCESS,
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


def global_saved_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Add Another",
                    callback_data="global:add",
                    style=ButtonStyle.SUCCESS,
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


async def global_manager_content(
    repository: GroupRepository,
) -> tuple[str, InlineKeyboardMarkup]:
    config = await repository.get_global_config()
    keyword_mode = config.get("reply_mode") == "keyword"
    responses = (
        await repository.get_global_keyword_responses()
        if keyword_mode
        else await repository.get_global_responses()
    )
    reactions = (
        await repository.get_global_keyword_reactions()
        if keyword_mode
        else config.get("reactions", [])
    )
    rate = config.get("rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE) or "∞"
    if keyword_mode:
        tuning = "🎯 Keyword mode: replies and reactions require matching keywords.\n\n"
    else:
        tuning = (
            f"💬 {config.get('reply_chance', DEFAULT_REPLY_CHANCE)}%  •  "
            f"🎲 {config.get('reaction_chance', DEFAULT_REACTION_CHANCE)}%  •  "
            f"⏱ {config.get('cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)}s  •  🚦 {rate}/min\n\n"
        )
    return (
        "🌐 Global Defaults\n\n"
        f"📚 Replies: {len(responses)}  •  🎭 Reactions: {len(reactions)}\n"
        f"{'🟢 New groups active' if config['enabled'] else '🔴 New groups paused'}\n"
        f"{tuning}"
        "These settings are inherited until a group overrides them.",
        global_manager_keyboard(config),
    )


async def manager_content(
    client: Client, repository: GroupRepository, chat_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    chat = await client.get_chat(chat_id)
    document = await repository.get(chat_id)
    keyword_mode = document.get("reply_mode") == "keyword"
    local_count = (
        len(document.get("keyword_responses", []))
        if keyword_mode
        else len(document["responses"])
    )
    keyword_reaction_count = len(document.get("keyword_reactions", []))
    if keyword_mode:
        global_count = (
            len(await repository.get_global_keyword_responses())
            if document.get("global_replies_enabled", True)
            else 0
        )
        global_reaction_count = (
            len(await repository.get_global_keyword_reactions())
            if document.get("global_reactions_enabled", True)
            else 0
        )
    else:
        global_count = (
            len(await repository.get_global_responses())
            if document.get("global_replies_enabled", True)
            else 0
        )
    rate = document.get("rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE) or "∞"
    if keyword_mode:
        text = (
            f"⚙️ {chat.title or chat_id}\n\n"
            f"{'🟢 Active' if document['enabled'] else '🔴 Paused'}  •  🎯 Keyword mode\n"
            f"📚 {local_count} local + {global_count} global replies  •  "
            f"🎭 {keyword_reaction_count} local + {global_reaction_count} global reactions\n"
            "Only matching keywords reply or react. Cooldown, chance, and rate limits are skipped in this mode."
        )
        return text, manager_keyboard(chat_id, document)
    text = (
        f"⚙️ {chat.title or chat_id}\n\n"
        f"{'🟢 Active' if document['enabled'] else '🔴 Paused'}  •  "
        f"📚 {local_count} local + {global_count} global\n"
        f"💬 {document.get('reply_chance', DEFAULT_REPLY_CHANCE)}%  •  "
        f"🎲 {document.get('reaction_chance', 25)}%  •  "
        f"⏱ {document.get('cooldown_seconds', DEFAULT_COOLDOWN_SECONDS)}s  •  🚦 {rate}/min"
        f"\n🌐 Inherited  •  🏠 Local overrides: {len(document.get('config_overrides', []))}"
    )
    return text, manager_keyboard(chat_id, document)


async def open_manager(
    client: Client, repository: GroupRepository, message: Message, chat_id: int
) -> None:
    if not message.from_user or not await user_is_group_admin(
        client, chat_id, message.from_user.id
    ):
        await message.reply_text("⛔ Group admins only.")
        return
    text, keyboard = await manager_content(client, repository, chat_id)
    await message.reply_text(text, reply_markup=keyboard)


async def require_admin(client: Client, message: Message) -> bool:
    try:
        allowed = await is_group_admin(client, message)
    except RPCError as exc:
        LOGGER.warning("Could not verify admin in chat %s: %s", message.chat.id, exc)
        await message.reply_text("⚠️ Promote me to admin, then try again.")
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
    await repository.ensure_group(message.chat.id)

    try:
        bot_member = await client.get_chat_member(message.chat.id, me.id)
        is_admin = bot_member.status in {
            ChatMemberStatus.OWNER,
            ChatMemberStatus.ADMINISTRATOR,
        }
    except RPCError as exc:
        LOGGER.warning(
            "Could not inspect bot permissions in chat %s: %s", message.chat.id, exc
        )
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


def register_handlers(
    app: Client, repository: GroupRepository, settings: Settings
) -> None:
    @app.on_message(filters.all, group=-1)
    async def record_interaction(_: Client, message: Message) -> None:
        if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await repository.ensure_group(message.chat.id)
        if message.from_user and not message.from_user.is_bot:
            await repository.record_user(
                message.from_user.id,
                private=message.chat.type == ChatType.PRIVATE,
            )

    @app.on_callback_query(group=-1)
    async def record_callback(_: Client, query: CallbackQuery) -> None:
        if query.from_user and not query.from_user.is_bot:
            await repository.record_user(query.from_user.id)

    @app.on_inline_query(group=-1)
    async def record_inline_query(_, query) -> None:
        if query.from_user and not query.from_user.is_bot:
            await repository.record_user(query.from_user.id)

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
                await message.reply_photo(
                    start_image, caption=START_TEXT, reply_markup=keyboard
                )
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

    @app.on_callback_query(filters.regex(r"^start:(sudo|commands|globals|stats|back)$"))
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
        if action == "commands":
            await show_callback_menu(
                query.message, SUDOER_COMMANDS_TEXT, sudoer_commands_keyboard()
            )
        elif action == "stats":
            stats = await repository.stats()
            await show_callback_menu(
                query.message,
                "📊 Bot Statistics\n\n"
                f"💬 Private chats: {stats['private_users']}\n"
                f"👤 Unique users: {stats['users']}\n"
                f"👥 Known groups: {stats['groups']}",
                sudoer_commands_keyboard(),
            )
        elif action == "globals":
            text, keyboard = await global_manager_content(repository)
            await query.message.reply_text(text, reply_markup=keyboard)
        else:
            await show_callback_menu(
                query.message, SUDOER_PANEL_TEXT, sudoer_panel_keyboard()
            )
        await query.answer()

    @app.on_message(filters.private & filters.command(["autoreply", "reaction"]))
    async def private_manager_hint(_: Client, message: Message) -> None:
        await message.reply_text("⚙️ Send /autoreply in your group.")

    @app.on_message(
        filters.private & filters.command(["sudos", "globals", "global_defaults"])
    )
    async def sudo_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("⛔ Sudoers only.")
            return
        command = message.command[0].lower()
        if command == "global_defaults":
            await message.reply_text("🌐 /global_defaults moved to /globals.")
        if command in {"globals", "global_defaults"}:
            text, keyboard = await global_manager_content(repository)
            await message.reply_text(text, reply_markup=keyboard)
            return
        await message.reply_text(
            SUDOER_PANEL_TEXT, reply_markup=sudoer_panel_keyboard()
        )

    @app.on_message(filters.private & filters.command("stats"))
    async def stats_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("⛔ Sudoers only.")
            return
        stats = await repository.stats()
        await message.reply_text(
            "📊 Bot Statistics\n\n"
            f"💬 Private chats: {stats['private_users']}\n"
            f"👤 Unique users: {stats['users']}\n"
            f"👥 Known groups: {stats['groups']}"
        )

    @app.on_message(filters.private & filters.command("broadcast"))
    async def broadcast_command(client: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("⛔ Sudoers only.")
            return
        response = broadcast_source(message)
        if response is None:
            await message.reply_text(
                "📣 Usage:\nReply to a message with /broadcast\n\nFlags: -copy, -user"
            )
            return
        target_count = len(await repository.group_ids())
        if isinstance(response, dict) and response.get("include_users"):
            target_count += len(await repository.user_ids())
        progress_message = await message.reply_text(
            broadcast_progress_text(0, target_count, 0, 0)
        )

        async def update_progress(
            processed: int, total: int, sent: int, failed: int
        ) -> None:
            try:
                await progress_message.edit_text(
                    broadcast_progress_text(processed, total, sent, failed)
                )
            except RPCError:
                LOGGER.warning("Could not update broadcast progress")

        sent, errors = await broadcast_response(
            client, repository, response, update_progress
        )
        try:
            await progress_message.edit_text(
                f"✅ Broadcast complete\n\n📨 Sent: {sent}\n⚠️ Failed: {len(errors)}"
            )
        except RPCError:
            LOGGER.warning("Could not update completed broadcast status")
        await send_broadcast_errors(message, errors)

    @app.on_message(
        filters.private & filters.command(["updates", "support", "owner_link"])
    )
    async def link_command(_: Client, message: Message) -> None:
        if not is_sudoer(settings, message):
            await message.reply_text("⛔ Sudoers only.")
            return

        name = message.command[0].lower()
        value = command_argument(message)
        labels = {
            "updates": "Updates channel",
            "support": "Support group",
            "owner_link": "Owner",
        }
        if not value:
            current = (await repository.get_links()).get(name, "not configured")
            await message.reply_text(
                f"🔗 {labels[name]}: {current}\n/{name} <url> or /{name} off"
            )
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
            LOGGER.exception(
                "Could not send group onboarding notice in chat %s", message.chat.id
            )

    @app.on_message(group_commands)
    async def handle_command(client: Client, message: Message) -> None:
        if not await require_admin(client, message):
            return
        command = message.command[0].lower()
        if command in {"delete_replies", "delete_all_replies"}:
            if command == "delete_all_replies":
                await repository.clear_all_responses(message.chat.id)
                text = "🧹 All random and keyword replies deleted."
            else:
                document = await repository.get(message.chat.id)
                if document.get("reply_mode") == "keyword":
                    for _ in range(len(document.get("keyword_responses", []))):
                        await repository.remove_keyword_response(message.chat.id, 1)
                    text = "🗑 Keyword replies deleted."
                else:
                    await repository.clear_responses(message.chat.id)
                    text = "🗑 Random replies deleted."
            notice = await message.reply_text(text)
            asyncio.create_task(delete_later(message, notice))
            return
        me = await client.get_me()
        launcher = await message.reply_text(
            "⚙️ Manage this group privately.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚙️ Open Manager",
                            url=f"https://t.me/{me.username}?start=configure_{message.chat.id}",
                            style=ButtonStyle.PRIMARY,
                        )
                    ]
                ]
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
            document = await repository.get(chat_id)
            if document.get("reply_mode") == "keyword":
                await repository.set_keyword_prompt(query.from_user.id, chat_id)
                await query.message.reply_text(
                    "🎯 Send the keyword or comma-separated keywords for this reply.\n\n/cancel to stop."
                )
            else:
                await repository.set_capture_group(query.from_user.id, chat_id)
                await query.message.reply_text(
                    "➕ Send the reply to save.\n\n/cancel to stop."
                )
            await query.answer("📥 Waiting for reply…")
            return
        if action in {"add-reaction", "add-keyword-reaction"}:
            document = await repository.get(chat_id)
            if document.get("reply_mode") == "keyword":
                await repository.set_keyword_prompt(
                    query.from_user.id, chat_id, reaction=True
                )
                await query.message.reply_text(
                    "🎯 Send the keyword or comma-separated keywords for this reaction.\n\n/cancel to stop."
                )
                await query.answer("📥 Waiting for keyword…")
            else:
                await repository.set_reaction_capture(query.from_user.id, chat_id)
                await query.message.reply_text(
                    "🎭 Send the reaction emoji to save.\n\n/cancel to stop."
                )
                await query.answer("📥 Waiting for reaction…")
            return
        if action == "toggle":
            document = await repository.get(chat_id)
            value = next_local_option(document, "enabled", [False, True])
            if value is None:
                await repository.clear_local_config(chat_id, "enabled")
            else:
                await repository.set_enabled(chat_id, value)
        elif action == "reactions":
            await repository.toggle_global_reactions(chat_id)
        elif action == "chance":
            document = await repository.get(chat_id)
            value = next_local_option(document, "reaction_chance", [0, 25, 50, 75, 100])
            if value is None:
                await repository.clear_local_config(chat_id, "reaction_chance")
            else:
                await repository.set_reaction_chance(chat_id, value)
        elif action == "reply-chance":
            document = await repository.get(chat_id)
            value = next_local_option(document, "reply_chance", [0, 25, 50, 75, 100])
            if value is None:
                await repository.clear_local_config(chat_id, "reply_chance")
            else:
                await repository.set_reply_chance(chat_id, value)
        elif action == "cooldown":
            document = await repository.get(chat_id)
            value = next_local_option(document, "cooldown_seconds", COOLDOWN_OPTIONS)
            if value is None:
                await repository.clear_local_config(chat_id, "cooldown_seconds")
            else:
                await repository.set_cooldown(chat_id, value)
        elif action == "rate":
            document = await repository.get(chat_id)
            value = next_local_option(
                document, "rate_limit_per_minute", RATE_LIMIT_OPTIONS
            )
            if value is None:
                await repository.clear_local_config(chat_id, "rate_limit_per_minute")
            else:
                await repository.set_rate_limit(chat_id, value)
        elif action == "globals":
            await repository.toggle_global_replies(chat_id)
            text, keyboard = await manager_content(client, repository, chat_id)
            await query.message.reply_text(text, reply_markup=keyboard)
            await query.answer("✅ Updated")
            return
        elif action == "mode":
            document = await repository.get(chat_id)
            value = next_local_option(document, "reply_mode", ["random", "keyword"])
            if value is None:
                await repository.clear_local_config(chat_id, "reply_mode")
            else:
                await repository.set_reply_mode(chat_id, value)
        elif action == "reaction-list":
            text, keyboard = await reaction_list_content(repository, chat_id)
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer()
            return
        elif action == "confirm-clear":
            await query.message.reply_text(
                "🗑 Clear replies?",
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
        elif action == "confirm-clear-reactions":
            await query.message.reply_text(
                "🎭 Clear reactions?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🎭 Clear",
                                callback_data=f"mgr:clear-reactions:{chat_id}",
                                style=ButtonStyle.DANGER,
                            ),
                            InlineKeyboardButton(
                                "✖️ Cancel", callback_data=f"mgr:open:{chat_id}"
                            ),
                        ]
                    ]
                ),
            )
            await query.answer()
            return
        elif action == "clear":
            document = await repository.get(chat_id)
            if document.get("reply_mode") == "keyword":
                for _ in range(len(document.get("keyword_responses", []))):
                    await repository.remove_keyword_response(chat_id, 1)
            else:
                await repository.clear_responses(chat_id)
        elif action == "clear-reactions":
            document = await repository.get(chat_id)
            if document.get("reply_mode") == "keyword":
                await repository.clear_keyword_reactions(chat_id)
            else:
                await repository.clear_reactions(chat_id)
        elif action.startswith("preview-"):
            try:
                source, raw_index, raw_page = action.removeprefix("preview-").split(
                    "-", 2
                )
                index = int(raw_index)
                page = int(raw_page)
                if source == "k":
                    entry = (await repository.get(chat_id)).get(
                        "keyword_responses", []
                    )[index - 1]
                    response = keyword_response(entry)
                elif source == "gk":
                    entry = (await repository.get_global_keyword_responses())[index - 1]
                    response = keyword_response(entry)
                else:
                    responses = (
                        (await repository.get(chat_id))["responses"]
                        if source == "l"
                        else await repository.get_global_responses()
                    )
                    response = responses[index - 1]
            except (ValueError, IndexError):
                await query.answer("⚠️ Reply not found.", show_alert=True)
                return
            await show_response_preview(
                client,
                query,
                response,
                f"{source.upper()}{index}",
                preview_keyboard(
                    f"mgr:list-{page}:{chat_id}",
                    "🗑 Delete"
                    if source in {"l", "k"}
                    else "🌐 Global Reply"
                    if source == "gk"
                    else "🚫 Exclude",
                    (
                        f"mgr:delete-keyword-{index}-{page}:{chat_id}"
                        if source == "k"
                        else f"mgr:delete-{index}-{page}:{chat_id}"
                        if source == "l"
                        else f"mgr:list-{page}:{chat_id}"
                        if source == "gk"
                        else f"mgr:exclude-{index}-{page}:{chat_id}"
                    ),
                ),
            )
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
        elif action.startswith("delete-keyword-"):
            try:
                index, page = callback_index_page(action, "delete-keyword-")
            except ValueError:
                await query.answer("⚠️ Invalid reply.", show_alert=True)
                return
            await repository.remove_keyword_response(chat_id, index)
            text, keyboard = await reply_list_content(client, repository, chat_id, page)
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer("🗑 Deleted")
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
            if (await repository.get_global_config()).get("reply_mode") == "keyword":
                await repository.set_global_keyword_prompt(query.from_user.id)
                await query.message.reply_text(
                    "🎯 Send the keyword or comma-separated keywords for this global reply.\n\n/cancel to stop."
                )
            else:
                await repository.set_global_capture(query.from_user.id)
                await query.message.reply_text(
                    "🌐 Send the global reply to save.\n\n/cancel to stop."
                )
            await query.answer("📥 Waiting for reply…")
            return
        if action == "add-reaction":
            if (await repository.get_global_config()).get("reply_mode") == "keyword":
                await repository.set_global_keyword_prompt(
                    query.from_user.id, reaction=True
                )
                await query.message.reply_text(
                    "🎯 Send the keyword or comma-separated keywords for this global reaction.\n\n/cancel to stop."
                )
                await query.answer("📥 Waiting for keyword…")
            else:
                await repository.set_reaction_capture(query.from_user.id, global_=True)
                await query.message.reply_text(
                    "🎭 Send the global reaction emoji to save.\n\n/cancel to stop."
                )
                await query.answer("📥 Waiting for reaction…")
            return
        if action == "reaction-list":
            text, keyboard = await global_reaction_list_content(repository)
            await query.message.edit_text(text, reply_markup=keyboard)
            await query.answer()
            return
        if action == "mode":
            config = await repository.get_global_config()
            await repository.set_global_config(
                "reply_mode",
                "random" if config.get("reply_mode") == "keyword" else "keyword",
            )
        if action in {
            "toggle",
            "reactions",
            "chance",
            "reply-chance",
            "cooldown",
            "rate",
        }:
            config = await repository.get_global_config()
            if action == "toggle":
                await repository.set_global_config("enabled", not config["enabled"])
            elif action == "reactions":
                await repository.set_global_config(
                    "reactions_enabled", not config.get("reactions_enabled", True)
                )
            elif action == "chance":
                await repository.set_global_config(
                    "reaction_chance",
                    (config.get("reaction_chance", DEFAULT_REACTION_CHANCE) + 25) % 125,
                )
            elif action == "reply-chance":
                await repository.set_global_config(
                    "reply_chance",
                    (config.get("reply_chance", DEFAULT_REPLY_CHANCE) + 25) % 125,
                )
            elif action == "cooldown":
                await repository.set_global_config(
                    "cooldown_seconds",
                    next_option(
                        config.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS),
                        COOLDOWN_OPTIONS,
                    ),
                )
            elif action == "rate":
                await repository.set_global_config(
                    "rate_limit_per_minute",
                    next_option(
                        config.get(
                            "rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE
                        ),
                        RATE_LIMIT_OPTIONS,
                    ),
                )
        elif action == "clear":
            if (await repository.get_global_config()).get("reply_mode") == "keyword":
                await repository.clear_global_keyword_responses()
            else:
                await repository.clear_global_responses()
        elif action == "clear-reactions":
            if (await repository.get_global_config()).get("reply_mode") == "keyword":
                await repository.clear_global_keyword_reactions()
            else:
                await repository.clear_global_reactions()
        elif action == "confirm-clear":
            await query.message.reply_text(
                "🗑 Clear global replies?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🗑 Clear",
                                callback_data="global:clear",
                                style=ButtonStyle.DANGER,
                            ),
                            InlineKeyboardButton(
                                "✖️ Cancel",
                                callback_data="global:open",
                                style=ButtonStyle.PRIMARY,
                            ),
                        ]
                    ]
                ),
            )
            await query.answer()
            return
        elif action == "confirm-clear-reactions":
            await query.message.reply_text(
                "🎭 Clear global reactions?",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🎭 Clear",
                                callback_data="global:clear-reactions",
                                style=ButtonStyle.DANGER,
                            ),
                            InlineKeyboardButton(
                                "✖️ Cancel", callback_data="global:open"
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
                if (await repository.get_global_config()).get(
                    "reply_mode"
                ) == "keyword":
                    entry = (await repository.get_global_keyword_responses())[index - 1]
                    response = keyword_response(entry)
                else:
                    response = (await repository.get_global_responses())[index - 1]
            except (ValueError, IndexError):
                await query.answer("⚠️ Reply not found.", show_alert=True)
                return
            await show_response_preview(
                client,
                query,
                response,
                f"Global Reply {index}",
                preview_keyboard(
                    f"global:list-{page}", "🗑 Delete", f"global:delete-{index}-{page}"
                ),
            )
            await query.answer()
            return
        elif action.startswith("delete-"):
            try:
                index, page = callback_index_page(action, "delete-")
            except ValueError:
                await query.answer("⚠️ Invalid reply.", show_alert=True)
                return
            if (await repository.get_global_config()).get("reply_mode") == "keyword":
                await repository.remove_global_keyword_response(index)
            else:
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
                "sudos",
                "globals",
                "global_defaults",
                "broadcast",
                "stats",
                "start_img",
            ]
        ),
        group=1,
    )
    async def capture_private_message(client: Client, message: Message) -> None:
        if not message.from_user:
            return
        state = await repository.get_capture_state(message.from_user.id)
        if state.get("capture_global_keyword_prompt"):
            keywords = split_keywords(message.text or message.caption or "")
            if not keywords:
                await message.reply_text(
                    "⚠️ Send at least one keyword, separated with commas if needed."
                )
                return
            await repository.set_capture_group(message.from_user.id, 0, keywords)
            if state.get("capture_global_reaction_prompt"):
                await repository.set_reaction_capture(
                    message.from_user.id, global_=True
                )
                await message.reply_text(
                    "🎭 Now send the global reaction emoji for those keywords.\n\n/cancel to stop."
                )
            else:
                await repository.set_global_capture(message.from_user.id)
                await message.reply_text(
                    "➕ Now send the global reply message for those keywords.\n\n/cancel to stop."
                )
            return
        if state.get("capture_keyword_prompt"):
            chat_id = state.get("capture_chat_id")
            keywords = split_keywords(message.text or message.caption or "")
            if not chat_id or not keywords:
                await message.reply_text(
                    "⚠️ Send at least one keyword, separated with commas if needed."
                )
                return
            await repository.set_capture_group(message.from_user.id, chat_id, keywords)
            if state.get("capture_reaction_prompt"):
                await repository.set_capture_reaction(message.from_user.id)
                await message.reply_text(
                    "🎭 Now send the reaction emoji for those keywords.\n\n/cancel to stop."
                )
            else:
                await message.reply_text(
                    "➕ Now send the reply message for those keywords.\n\n/cancel to stop."
                )
            return
        global_capture = settings.is_sudoer(
            message.from_user.id
        ) and await repository.is_global_capture(message.from_user.id)
        global_reaction_capture = settings.is_sudoer(
            message.from_user.id
        ) and state.get("capture_global_reaction")
        chat_id = state.get("capture_chat_id") or await repository.get_capture_group(
            message.from_user.id
        )
        if chat_id is None and not global_capture and not global_reaction_capture:
            return
        if (
            not global_capture
            and not global_reaction_capture
            and not await user_is_group_admin(client, chat_id, message.from_user.id)
        ):
            await repository.clear_capture_group(message.from_user.id)
            await message.reply_text("⛔ Group admins only.")
            return
        if state.get("capture_reaction"):
            reaction = (message.text or message.caption or "").strip()
            if not reaction:
                await message.reply_text("⚠️ Send the reaction as text.")
                return
            if not await validate_reaction(message, reaction):
                await message.reply_text("⚠️ Send a valid Telegram reaction emoji.")
                return
            if state.get("capture_global_reaction"):
                result = (
                    await repository.add_global_keyword_reaction(
                        state.get("capture_keywords", []),
                        reaction,
                    )
                    if state.get("capture_keywords")
                    else await repository.add_global_reaction(reaction)
                )
            elif state.get("capture_keywords"):
                result = await repository.add_keyword_reaction(
                    chat_id,
                    state.get("capture_keywords", []),
                    reaction,
                )
            else:
                result = await repository.add_reaction(chat_id, reaction)
            await repository.clear_capture_group(message.from_user.id)
            target = (
                "Global reaction"
                if state.get("capture_global_reaction")
                else "Reaction"
            )
            await message.reply_text(
                f"✅ {target} saved." if result == "added" else "⚠️ Already saved.",
                reply_markup=(
                    global_saved_keyboard()
                    if state.get("capture_global_reaction") and result == "added"
                    else saved_reply_keyboard(chat_id)
                    if result == "added"
                    else None
                ),
            )
            return
        source = message
        if settings.storage_chat_id:
            try:
                source = await client.copy_message(
                    settings.storage_chat_id, message.chat.id, message.id
                )
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
            await repository.add_global_keyword_response(
                state.get("capture_keywords", []), response
            )
            if global_capture and state.get("capture_keywords")
            else await repository.add_global_response(response)
            if global_capture
            else (
                await repository.add_keyword_response(
                    chat_id, state.get("capture_keywords", []), response
                )
                if state.get("capture_keywords")
                else await repository.add_response(chat_id, response)
            )
        )
        await repository.clear_capture_group(message.from_user.id)
        replies = {
            "added": "✅ Reply saved.",
            "duplicate": "⚠️ Already saved.",
            "missing_keyword": "⚠️ Add a keyword first.",
        }
        if state.get("capture_keywords"):
            replies["added"] = "✅ Keyword reply saved."
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
        if not group_settings["enabled"]:
            return
        keyword_mode = group_settings.get("reply_mode") == "keyword"
        incoming_text = message.text or message.caption or ""
        if keyword_mode:
            response = await repository.keyword_response(message.chat.id, incoming_text)
        else:
            if not interaction_allowed(
                message.chat.id,
                group_settings.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS),
                group_settings.get(
                    "rate_limit_per_minute", DEFAULT_RATE_LIMIT_PER_MINUTE
                ),
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

        if keyword_mode:
            reaction = await repository.keyword_reaction(message.chat.id, incoming_text)
        else:
            reaction_settings = await repository.reaction_settings(message.chat.id)
            reaction = (
                choose_reaction(*reaction_settings) if reaction_settings else None
            )
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
