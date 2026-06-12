import logging
import random

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ChatType, ParseMode
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Message

from autoreply.config import Settings
from autoreply.repository import GroupRepository, MAX_REACTIONS, MAX_RESPONSES


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
    BotCommand("autoreply", "Show all available commands"),
    BotCommand("reaction", "Manage reactions: /reaction help"),
    BotCommand("updates", "Owner: configure updates button"),
    BotCommand("support", "Owner: configure support button"),
    BotCommand("owner_link", "Owner: configure owner button"),
]
START_TEXT = (
    "Telegram Group Interaction Bot\n\n"
    "I can keep group chats active with rotating automatic replies and occasional random reactions.\n\n"
    "Quick setup:\n"
    "1. Add me to your group as an administrator.\n"
    "2. Disable privacy mode through BotFather so I can see group messages.\n"
    "3. Add a reply with /autoreply add <message>.\n"
    "4. Enable interactions with /autoreply on.\n\n"
    "Use /autoreply in the group to view every available command."
)
COMMAND_CATALOG = (
    "Available commands:\n\n"
    "Replies\n"
    "/autoreply on - enable interactions\n"
    "/autoreply off - disable interactions\n"
    "/autoreply add <message> - add a response\n"
    "/autoreply remove <number> - remove a response\n"
    "/autoreply list - list responses\n"
    "/autoreply clear - remove all responses\n"
    "/autoreply status - show current status\n\n"
    "Reactions\n"
    "/reaction on - enable random reactions\n"
    "/reaction off - disable random reactions\n"
    "/reaction chance <0-100> - set probability\n"
    "/reaction add <emoji> - add a reaction\n"
    "/reaction remove <emoji> - remove a reaction\n"
    "/reaction list - list reactions\n\n"
    "Only group administrators can change settings."
)


def command_argument(message: Message) -> str:
    text = message.text or ""
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


def command_action(message: Message) -> tuple[str, str]:
    argument = command_argument(message)
    parts = argument.split(maxsplit=1)
    return (parts[0].lower(), parts[1].strip() if len(parts) == 2 else "") if parts else ("", "")


def choose_reaction(chance: int, reactions: list[str]) -> str | None:
    if not reactions or random.randint(1, 100) > chance:
        return None
    return random.choice(reactions)


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


async def is_group_admin(client: Client, message: Message) -> bool:
    if not message.from_user:
        return False
    member = await client.get_chat_member(message.chat.id, message.from_user.id)
    return member.status in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}


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
    async def private_help(_: Client, message: Message) -> None:
        links = await repository.get_links()
        await message.reply_text(START_TEXT, reply_markup=link_keyboard(links))

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
        command = message.command[0].lower()
        action, value = command_action(message)

        if command == "autoreply" and action in {"", "help"}:
            await message.reply_text(COMMAND_CATALOG)
            return

        if not await require_admin(client, message):
            return

        chat_id = message.chat.id

        if command == "autoreply":
            if action in {"on", "off"}:
                enabled = action == "on"
                await repository.set_enabled(chat_id, enabled)
                if enabled:
                    settings = await repository.get(chat_id)
                    response_count = len(settings["responses"])
                    note = (
                        "\nNo reply messages are configured yet. Use /autoreply add <message>."
                        if response_count == 0
                        else f"\nConfigured reply messages: {response_count}."
                    )
                    await message.reply_text(
                        "Interactions are now enabled."
                        f"\nRandom reaction chance: {settings['reaction_chance']}%."
                        f"{note}"
                    )
                else:
                    await message.reply_text("Interactions are now disabled.")
            elif action == "add":
                if not value:
                    await message.reply_text("Usage: /autoreply add <message>")
                elif len(value) > 4000:
                    await message.reply_text("Responses must be 4,000 characters or fewer.")
                else:
                    result = await repository.add_response(chat_id, value)
                    replies = {
                        "added": "Response added.",
                        "duplicate": "That response already exists.",
                        "full": f"This group already has the maximum of {MAX_RESPONSES} responses.",
                    }
                    await message.reply_text(replies[result])
            elif action == "remove":
                if not value.isdigit():
                    await message.reply_text("Usage: /autoreply remove <number>")
                else:
                    removed = await repository.remove_response(chat_id, int(value))
                    await message.reply_text(
                        "Response removed." if removed else "Response number not found."
                    )
            elif action == "list":
                document = await repository.get(chat_id)
                responses = document["responses"]
                if not responses:
                    await message.reply_text("No responses are configured.")
                else:
                    lines = [f"{index}. {text}" for index, text in enumerate(responses, start=1)]
                    await message.reply_text(("Configured responses:\n" + "\n".join(lines))[:4096])
            elif action == "clear":
                count = await repository.clear_responses(chat_id)
                await message.reply_text(f"Removed {count} response(s).")
            elif action == "status":
                document = await repository.get(chat_id)
                await message.reply_text(
                    f"Status: {'enabled' if document['enabled'] else 'disabled'}\n"
                    f"Responses: {len(document['responses'])}\n"
                    f"Reply mode: rotate in order\n"
                    f"Reactions: {'enabled' if document.get('reactions_enabled', True) else 'disabled'}\n"
                    f"Reaction chance: {document.get('reaction_chance', 25)}%"
                )
            else:
                await message.reply_text("Unknown action. Use /autoreply to view available commands.")
        elif command == "reaction" and action in {"", "help"}:
            await message.reply_text(
                "Reaction commands:\n"
                "/reaction on - enable random reactions\n"
                "/reaction off - disable random reactions\n"
                "/reaction chance <0-100> - set probability\n"
                "/reaction add <emoji> - add a reaction\n"
                "/reaction remove <emoji> - remove a reaction\n"
                "/reaction list - list reactions"
            )
        elif command == "reaction":
            if action in {"on", "off"}:
                enabled = action == "on"
                await repository.set_reactions_enabled(chat_id, enabled)
                await message.reply_text(
                    f"Random reactions are now {'enabled' if enabled else 'disabled'}."
                )
            elif action == "chance":
                if not value.isdigit() or not 0 <= int(value) <= 100:
                    await message.reply_text("Usage: /reaction chance <0-100>")
                else:
                    await repository.set_reaction_chance(chat_id, int(value))
                    await message.reply_text(f"Reaction chance set to {value}%.")
            elif action == "add":
                if not value or len(value) > 16 or " " in value:
                    await message.reply_text("Usage: /reaction add <emoji>")
                else:
                    result = await repository.add_reaction(chat_id, value)
                    replies = {
                        "added": "Reaction added.",
                        "duplicate": "That reaction already exists.",
                        "full": f"This group already has the maximum of {MAX_REACTIONS} reactions.",
                    }
                    await message.reply_text(replies[result])
            elif action == "remove":
                if not value:
                    await message.reply_text("Usage: /reaction remove <emoji>")
                else:
                    removed = await repository.remove_reaction(chat_id, value)
                    await message.reply_text("Reaction removed." if removed else "Reaction not found.")
            elif action == "list":
                document = await repository.get(chat_id)
                reactions = document.get("reactions", [])
                output = "Configured reactions: " + (" ".join(reactions) if reactions else "none")
                await message.reply_text(output)
            else:
                await message.reply_text("Unknown action. Use /reaction help.")

    async def eligible_message(_, __, message: Message) -> bool:
        return bool(
            message.from_user
            and not message.from_user.is_bot
            and message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
            and not (message.text or "").startswith("/")
            and not message.service
        )

    @app.on_message(filters.create(eligible_message), group=1)
    async def handle_group_message(_: Client, message: Message) -> None:
        response = await repository.next_response(message.chat.id)
        if response:
            try:
                await message.reply_text(response)
            except FloodWait as exc:
                LOGGER.warning("Reply flood wait for %s seconds in chat %s", exc.value, message.chat.id)
            except RPCError:
                LOGGER.exception("Could not reply in chat %s", message.chat.id)

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
