from dataclasses import dataclass
import os

from dotenv import load_dotenv


def parse_id_list(value: str | None) -> tuple[int, ...]:
    if not value:
        return ()
    raw_ids = value.replace(",", " ").split()
    try:
        return tuple(dict.fromkeys(int(raw_id) for raw_id in raw_ids))
    except ValueError as exc:
        raise RuntimeError("SUDOER_IDS must contain only integer Telegram user IDs") from exc


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    mongodb_uri: str
    mongodb_database: str
    owner_id: int
    sudoer_ids: tuple[int, ...]
    storage_chat_id: int | None

    def is_sudoer(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in {self.owner_id, *self.sudoer_ids}

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        required = {
            "TELEGRAM_API_ID": os.getenv("TELEGRAM_API_ID"),
            "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH"),
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "MONGODB_URI": os.getenv("MONGODB_URI"),
            "OWNER_ID": os.getenv("OWNER_ID"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        try:
            api_id = int(required["TELEGRAM_API_ID"])
            owner_id = int(required["OWNER_ID"])
            sudoer_ids = parse_id_list(os.getenv("SUDOER_IDS"))
            storage_chat_id = int(os.environ["STORAGE_CHAT_ID"]) if os.getenv("STORAGE_CHAT_ID") else None
        except ValueError as exc:
            raise RuntimeError("TELEGRAM_API_ID, OWNER_ID, and STORAGE_CHAT_ID must be integers") from exc
        return cls(
            api_id=api_id,
            api_hash=required["TELEGRAM_API_HASH"],
            bot_token=required["TELEGRAM_BOT_TOKEN"],
            mongodb_uri=required["MONGODB_URI"],
            mongodb_database=os.getenv("MONGODB_DATABASE", "telegram_autoreply"),
            owner_id=owner_id,
            sudoer_ids=sudoer_ids,
            storage_chat_id=storage_chat_id,
        )
