import logging
import random

from pyrogram import Client, filters, idle
from pyrogram.enums import ChatMemberStatus, ChatType
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import BotCommand, Message

from autoreply.config import Settings
from autoreply.repository import GroupRepository, MAX_REACTIONS, MAX_RESPONSES


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger(__name__)
COMMANDS = [
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
]
BOT_MENU_COMMANDS = [
    BotCommand("start", "Show setup instructions"),
    BotCommand("help", "Show setup instructions"),
    BotCommand("autoreply", "Enable or disable: /autoreply on|off"),
    BotCommand("autoreply_add", "Add a reply message"),
    BotCommand("autoreply_remove", "Remove a reply by number"),
    BotCommand("autoreply_list", "List reply messages"),
    BotCommand("autoreply_clear", "Remove all reply messages"),
    BotCommand("autoreply_status", "Show interaction status"),
    BotCommand("autoreply_help", "Show all commands"),
    BotCommand("reaction_on", "Enable random reactions"),
    BotCommand("reaction_off", "Disable random reactions"),
    BotCommand("reaction_chance", "Set reaction chance from 0 to 100"),
    BotCommand("reaction_add", "Add a reaction emoji"),
    BotCommand("reaction_remove", "Remove a reaction emoji"),
    BotCommand("reaction_list", "List reaction emojis"),
]


def command_argument(message: Message) -> str:
    text = message.text or ""
    return text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""


def choose_reaction(chance: int, reactions: list[str]) -> str | None:
    if not reactions or random.randint(1, 100) > chance:
        return None
    return random.choice(reactions)


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


def register_handlers(app: Client, repository: GroupRepository) -> None:
    @app.on_message(filters.private & filters.command(["start", "help"]))
    async def private_help(_: Client, message: Message) -> None:
        await message.reply_text(
            "Add me to a group as an administrator and disable privacy mode with BotFather.\n\n"
            "Then use /autoreply on and /autoreply_add <message> in the group."
        )

    group_commands = filters.group & filters.command(COMMANDS)

    @app.on_message(group_commands)
    async def handle_command(client: Client, message: Message) -> None:
        if not await require_admin(client, message):
            return

        command = message.command[0].lower()
        chat_id = message.chat.id

        if command == "autoreply_help":
            await message.reply_text(
                "Admin commands:\n"
                "/autoreply on - enable interactions\n"
                "/autoreply off - disable interactions\n"
                "/autoreply_add <message> - add a response\n"
                "/autoreply_remove <number> - remove a response\n"
                "/autoreply_list - list responses\n"
                "/autoreply_clear - remove all responses\n"
                "/autoreply_status - show current status\n"
                "/reaction_on and /reaction_off - toggle reactions\n"
                "/reaction_chance <0-100> - set reaction probability\n"
                "/reaction_add <emoji> - add a reaction\n"
                "/reaction_remove <emoji> - remove a reaction\n"
                "/reaction_list - list reactions"
            )
        elif command == "autoreply":
            action = command_argument(message).lower()
            if action not in {"on", "off"}:
                await message.reply_text("Usage: /autoreply on or /autoreply off")
            else:
                enabled = action == "on"
                await repository.set_enabled(chat_id, enabled)
                if enabled:
                    settings = await repository.get(chat_id)
                    response_count = len(settings["responses"])
                    note = (
                        "\nNo reply messages are configured yet. Use /autoreply_add <message>."
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
        elif command == "autoreply_add":
            response = command_argument(message)
            if not response:
                await message.reply_text("Usage: /autoreply_add <message>")
            elif len(response) > 4000:
                await message.reply_text("Responses must be 4,000 characters or fewer.")
            else:
                result = await repository.add_response(chat_id, response)
                replies = {
                    "added": "Response added.",
                    "duplicate": "That response already exists.",
                    "full": f"This group already has the maximum of {MAX_RESPONSES} responses.",
                }
                await message.reply_text(replies[result])
        elif command == "autoreply_remove":
            argument = command_argument(message)
            if not argument.isdigit():
                await message.reply_text("Usage: /autoreply_remove <number>")
            else:
                removed = await repository.remove_response(chat_id, int(argument))
                await message.reply_text("Response removed." if removed else "Response number not found.")
        elif command == "autoreply_list":
            document = await repository.get(chat_id)
            responses = document["responses"]
            if not responses:
                await message.reply_text("No responses are configured.")
            else:
                lines = [f"{index}. {text}" for index, text in enumerate(responses, start=1)]
                output = "Configured responses:\n" + "\n".join(lines)
                await message.reply_text(output[:4096])
        elif command == "autoreply_clear":
            count = await repository.clear_responses(chat_id)
            await message.reply_text(f"Removed {count} response(s).")
        elif command == "autoreply_status":
            document = await repository.get(chat_id)
            await message.reply_text(
                f"Status: {'enabled' if document['enabled'] else 'disabled'}\n"
                f"Responses: {len(document['responses'])}\n"
                f"Reply mode: rotate in order\n"
                f"Reactions: {'enabled' if document.get('reactions_enabled', True) else 'disabled'}\n"
                f"Reaction chance: {document.get('reaction_chance', 25)}%"
            )
        elif command in {"reaction_on", "reaction_off"}:
            enabled = command == "reaction_on"
            await repository.set_reactions_enabled(chat_id, enabled)
            await message.reply_text(f"Random reactions are now {'enabled' if enabled else 'disabled'}.")
        elif command == "reaction_chance":
            argument = command_argument(message)
            if not argument.isdigit() or not 0 <= int(argument) <= 100:
                await message.reply_text("Usage: /reaction_chance <0-100>")
            else:
                await repository.set_reaction_chance(chat_id, int(argument))
                await message.reply_text(f"Reaction chance set to {argument}%.")
        elif command == "reaction_add":
            reaction = command_argument(message)
            if not reaction or len(reaction) > 16 or " " in reaction:
                await message.reply_text("Usage: /reaction_add <emoji>")
            else:
                result = await repository.add_reaction(chat_id, reaction)
                replies = {
                    "added": "Reaction added.",
                    "duplicate": "That reaction already exists.",
                    "full": f"This group already has the maximum of {MAX_REACTIONS} reactions.",
                }
                await message.reply_text(replies[result])
        elif command == "reaction_remove":
            reaction = command_argument(message)
            if not reaction:
                await message.reply_text("Usage: /reaction_remove <emoji>")
            else:
                removed = await repository.remove_reaction(chat_id, reaction)
                await message.reply_text("Reaction removed." if removed else "Reaction not found.")
        elif command == "reaction_list":
            document = await repository.get(chat_id)
            reactions = document.get("reactions", [])
            output = "Configured reactions: " + (" ".join(reactions) if reactions else "none")
            await message.reply_text(output)

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
    )
    register_handlers(app, repository)

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
