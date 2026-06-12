from dataclasses import dataclass
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    bot_token: str
    mongodb_uri: str
    mongodb_database: str
    updates: str | None
    support: str | None
    owner_link: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        required = {
            "TELEGRAM_API_ID": os.getenv("TELEGRAM_API_ID"),
            "TELEGRAM_API_HASH": os.getenv("TELEGRAM_API_HASH"),
            "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN"),
            "MONGODB_URI": os.getenv("MONGODB_URI"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        try:
            api_id = int(required["TELEGRAM_API_ID"])
        except ValueError as exc:
            raise RuntimeError("TELEGRAM_API_ID must be an integer") from exc

        return cls(
            api_id=api_id,
            api_hash=required["TELEGRAM_API_HASH"],
            bot_token=required["TELEGRAM_BOT_TOKEN"],
            mongodb_uri=required["MONGODB_URI"],
            mongodb_database=os.getenv("MONGODB_DATABASE", "telegram_autoreply"),
            updates=os.getenv("UPDATES"),
            support=os.getenv("SUPPORT"),
            owner_link=os.getenv("OWNER_LINK"),
        )
