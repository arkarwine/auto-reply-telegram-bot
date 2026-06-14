from typing import Any
import random

from pymongo import AsyncMongoClient


MAX_RESPONSES = 100
MAX_REACTIONS = 20
DEFAULT_REACTIONS = ["👍", "❤️", "😂", "🎉", "👀"]
DEFAULT_REACTION_CHANCE = 25
DEFAULT_REPLY_CHANCE = 50
DEFAULT_COOLDOWN_SECONDS = 10
DEFAULT_RATE_LIMIT_PER_MINUTE = 0
DEFAULT_MENTION_LIMIT = 100
DEFAULT_MENTION_BATCH_SIZE = 5
DEFAULT_MENTION_DELAY_SECONDS = 2
GLOBAL_CONFIG_KEYS = (
    "enabled",
    "reply_chance",
    "cooldown_seconds",
    "rate_limit_per_minute",
    "reactions_enabled",
    "reaction_chance",
)


class GroupRepository:
    def __init__(self, mongodb_uri: str, database_name: str) -> None:
        self.client = AsyncMongoClient(mongodb_uri)
        self.collection = self.client[database_name]["groups"]
        self.settings_collection = self.client[database_name]["bot_settings"]
        self.states_collection = self.client[database_name]["user_states"]
        self.members_collection = self.client[database_name]["group_members"]

    async def ping(self) -> None:
        await self.client.admin.command("ping")

    async def close(self) -> None:
        await self.client.close()

    async def get_global_config(self) -> dict[str, Any]:
        document = await self.settings_collection.find_one({"_id": "global_config"})
        defaults = {
            "enabled": True,
            "reply_chance": DEFAULT_REPLY_CHANCE,
            "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
            "rate_limit_per_minute": DEFAULT_RATE_LIMIT_PER_MINUTE,
            "reactions_enabled": True,
            "reaction_chance": DEFAULT_REACTION_CHANCE,
        }
        return defaults | document if document else defaults

    async def set_global_config(self, name: str, value: Any) -> None:
        await self.settings_collection.update_one(
            {"_id": "global_config"},
            {"$set": {name: value}},
            upsert=True,
        )

    async def ensure_group(self, chat_id: int) -> None:
        await self.collection.update_one(
            {"_id": chat_id},
            {
                "$setOnInsert": {
                    "responses": [],
                    "global_replies_enabled": True,
                    "excluded_global_responses": [],
                    "reactions": DEFAULT_REACTIONS,
                    "config_overrides": [],
                    "mention_limit": DEFAULT_MENTION_LIMIT,
                    "mention_batch_size": DEFAULT_MENTION_BATCH_SIZE,
                    "mention_delay_seconds": DEFAULT_MENTION_DELAY_SECONDS,
                }
            },
            upsert=True,
        )

    async def get(self, chat_id: int) -> dict[str, Any]:
        document = await self.collection.find_one({"_id": chat_id})
        global_config = await self.get_global_config()
        defaults = {
            "_id": chat_id,
            "enabled": global_config["enabled"],
            "responses": [],
            "next_index": 0,
            "reply_chance": global_config["reply_chance"],
            "cooldown_seconds": global_config["cooldown_seconds"],
            "rate_limit_per_minute": global_config["rate_limit_per_minute"],
            "global_replies_enabled": True,
            "excluded_global_responses": [],
            "reactions_enabled": global_config["reactions_enabled"],
            "reaction_chance": global_config["reaction_chance"],
            "reactions": list(DEFAULT_REACTIONS),
            "config_overrides": [],
            "mention_limit": DEFAULT_MENTION_LIMIT,
            "mention_batch_size": DEFAULT_MENTION_BATCH_SIZE,
            "mention_delay_seconds": DEFAULT_MENTION_DELAY_SECONDS,
        }
        if not document:
            return defaults
        overrides = set(document.get("config_overrides", []))
        # Preserve disabled legacy groups, including groups disabled after permission errors.
        if document.get("enabled") is False:
            overrides.add("enabled")
        effective = defaults | {
            key: value for key, value in document.items() if key not in GLOBAL_CONFIG_KEYS
        }
        for key in overrides:
            if key in document:
                effective[key] = document[key]
        effective["config_overrides"] = list(overrides)
        return effective

    async def set_local_config(self, chat_id: int, name: str, value: Any) -> None:
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {name: value}, "$addToSet": {"config_overrides": name}},
            upsert=True,
        )

    async def clear_local_config(self, chat_id: int, name: str | None = None) -> None:
        if name:
            update = {"$pull": {"config_overrides": name}, "$unset": {name: ""}}
        else:
            update = {
                "$set": {"config_overrides": []},
                "$unset": {key: "" for key in GLOBAL_CONFIG_KEYS},
            }
        await self.collection.update_one({"_id": chat_id}, update, upsert=True)

    async def set_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.ensure_group(chat_id)
        await self.set_local_config(chat_id, "enabled", enabled)

    async def group_ids(self) -> list[int]:
        return [document["_id"] async for document in self.collection.find({}, {"_id": 1})]

    async def record_member(
        self,
        chat_id: int,
        user_id: int,
        first_name: str | None,
        username: str | None,
    ) -> None:
        await self.members_collection.update_one(
            {"_id": f"{chat_id}:{user_id}"},
            {
                "$set": {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "first_name": first_name or "Member",
                    "username": username,
                }
            },
            upsert=True,
        )

    async def get_members(self, chat_id: int, limit: int) -> list[dict[str, Any]]:
        cursor = self.members_collection.find({"chat_id": chat_id}).limit(limit)
        return [member async for member in cursor]

    async def member_count(self, chat_id: int) -> int:
        return await self.members_collection.count_documents({"chat_id": chat_id})

    async def remove_member(self, chat_id: int, user_id: int) -> None:
        await self.members_collection.delete_one({"_id": f"{chat_id}:{user_id}"})

    async def set_mention_setting(self, chat_id: int, name: str, value: int) -> None:
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {name: value}},
            upsert=True,
        )

    async def add_response(self, chat_id: int, response: Any) -> str:
        document = await self.get(chat_id)
        global_config = await self.get_global_config()
        responses = document["responses"]
        if response in responses:
            return "duplicate"
        if len(responses) >= MAX_RESPONSES:
            return "full"

        await self.collection.update_one(
            {"_id": chat_id},
            {
                "$push": {"responses": response},
                "$setOnInsert": {
                    "enabled": global_config["enabled"],
                    "next_index": 0,
                    "reply_chance": global_config["reply_chance"],
                    "cooldown_seconds": global_config["cooldown_seconds"],
                    "rate_limit_per_minute": global_config["rate_limit_per_minute"],
                    "global_replies_enabled": True,
                    "excluded_global_responses": [],
                    "reactions_enabled": global_config["reactions_enabled"],
                    "reaction_chance": global_config["reaction_chance"],
                    "reactions": DEFAULT_REACTIONS,
                },
            },
            upsert=True,
        )
        return "added"

    async def remove_response(self, chat_id: int, one_based_index: int) -> Any | None:
        document = await self.get(chat_id)
        responses = document["responses"]
        if one_based_index < 1 or one_based_index > len(responses):
            return None

        removed = responses.pop(one_based_index - 1)
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"responses": responses, "next_index": 0}},
            upsert=True,
        )
        return removed

    async def clear_responses(self, chat_id: int) -> int:
        document = await self.get(chat_id)
        count = len(document["responses"])
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"responses": [], "next_index": 0}},
            upsert=True,
        )
        return count

    async def next_response(self, chat_id: int) -> Any | None:
        global_responses = await self.get_global_responses()
        document = await self.get(chat_id)
        if not document["enabled"]:
            return None

        if not document.get("global_replies_enabled", True):
            global_responses = []
        else:
            excluded = document.get("excluded_global_responses", [])
            global_responses = [response for response in global_responses if response not in excluded]
        responses = document.get("responses", []) + global_responses
        if not responses:
            return None
        return random.choice(responses)

    async def reply_chance(self, chat_id: int) -> int | None:
        document = await self.collection.find_one(
            {"_id": chat_id, "enabled": True},
            {"reply_chance": 1},
        )
        return document.get("reply_chance", DEFAULT_REPLY_CHANCE) if document else None

    async def set_reply_chance(self, chat_id: int, chance: int) -> None:
        await self.set_local_config(chat_id, "reply_chance", chance)

    async def set_cooldown(self, chat_id: int, seconds: int) -> None:
        await self.set_local_config(chat_id, "cooldown_seconds", seconds)

    async def set_rate_limit(self, chat_id: int, per_minute: int) -> None:
        await self.set_local_config(chat_id, "rate_limit_per_minute", per_minute)

    async def toggle_global_replies(self, chat_id: int) -> bool:
        document = await self.get(chat_id)
        enabled = not document.get("global_replies_enabled", True)
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"global_replies_enabled": enabled}},
            upsert=True,
        )
        return enabled

    async def toggle_global_exclusion(self, chat_id: int, response: Any) -> bool:
        document = await self.get(chat_id)
        excluded = list(document.get("excluded_global_responses", []))
        if response in excluded:
            excluded.remove(response)
            is_excluded = False
        else:
            excluded.append(response)
            is_excluded = True
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"excluded_global_responses": excluded}},
            upsert=True,
        )
        return is_excluded

    async def get_global_responses(self) -> list[Any]:
        document = await self.settings_collection.find_one({"_id": "global_responses"})
        return document.get("responses", []) if document else []

    async def add_global_response(self, response: Any) -> str:
        responses = await self.get_global_responses()
        if response in responses:
            return "duplicate"
        if len(responses) >= MAX_RESPONSES:
            return "full"
        await self.settings_collection.update_one(
            {"_id": "global_responses"},
            {"$push": {"responses": response}},
            upsert=True,
        )
        await self.collection.update_many(
            {"excluded_global_responses": response},
            {"$pull": {"excluded_global_responses": response}},
        )
        return "added"

    async def remove_global_response(self, one_based_index: int) -> Any | None:
        responses = await self.get_global_responses()
        if one_based_index < 1 or one_based_index > len(responses):
            return None
        removed = responses.pop(one_based_index - 1)
        await self.settings_collection.update_one(
            {"_id": "global_responses"},
            {"$set": {"responses": responses}},
            upsert=True,
        )
        return removed

    async def clear_global_responses(self) -> int:
        responses = await self.get_global_responses()
        await self.settings_collection.update_one(
            {"_id": "global_responses"},
            {"$set": {"responses": []}},
            upsert=True,
        )
        return len(responses)

    async def set_reactions_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.set_local_config(chat_id, "reactions_enabled", enabled)

    async def set_reaction_chance(self, chat_id: int, chance: int) -> None:
        await self.set_local_config(chat_id, "reaction_chance", chance)

    async def add_reaction(self, chat_id: int, reaction: str) -> str:
        document = await self.get(chat_id)
        reactions = list(document.get("reactions", DEFAULT_REACTIONS))
        if reaction in reactions:
            return "duplicate"
        if len(reactions) >= MAX_REACTIONS:
            return "full"

        reactions.append(reaction)
        await self.collection.update_one(
            {"_id": chat_id},
            {
                "$set": {"reactions": reactions},
                "$setOnInsert": {
                    "enabled": True,
                    "responses": [],
                    "next_index": 0,
                    "reply_chance": DEFAULT_REPLY_CHANCE,
                    "reactions_enabled": True,
                    "reaction_chance": DEFAULT_REACTION_CHANCE,
                },
            },
            upsert=True,
        )
        return "added"

    async def remove_reaction(self, chat_id: int, reaction: str) -> bool:
        document = await self.get(chat_id)
        reactions = list(document.get("reactions", DEFAULT_REACTIONS))
        if reaction not in reactions:
            return False
        reactions.remove(reaction)
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"reactions": reactions}},
            upsert=True,
        )
        return True

    async def reaction_settings(self, chat_id: int) -> tuple[int, list[str]] | None:
        document = await self.get(chat_id)
        if not document["enabled"] or not document.get("reactions_enabled", True):
            return None
        return (
            document.get("reaction_chance", DEFAULT_REACTION_CHANCE),
            document.get("reactions", DEFAULT_REACTIONS),
        )

    async def get_links(self) -> dict[str, str]:
        document = await self.settings_collection.find_one({"_id": "links"})
        if not document:
            return {}
        return {
            key: document[key]
            for key in ("updates", "support", "owner_link")
            if document.get(key)
        }

    async def set_link(self, name: str, url: str | None) -> None:
        update = {"$set": {name: url}} if url else {"$unset": {name: ""}}
        await self.settings_collection.update_one({"_id": "links"}, update, upsert=True)

    async def get_start_image(self) -> str | None:
        document = await self.settings_collection.find_one({"_id": "start_image"})
        return document.get("file_id") if document else None

    async def set_start_image(self, file_id: str | None) -> None:
        if file_id:
            await self.settings_collection.update_one(
                {"_id": "start_image"},
                {"$set": {"file_id": file_id}},
                upsert=True,
            )
        else:
            await self.settings_collection.delete_one({"_id": "start_image"})

    async def set_capture_group(self, user_id: int, chat_id: int) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {
                "$set": {"capture_chat_id": chat_id},
                "$unset": {"capture_global": "", "capture_broadcast": "", "pending_broadcast": ""},
            },
            upsert=True,
        )

    async def set_global_capture(self, user_id: int) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {
                "$set": {"capture_global": True},
                "$unset": {"capture_chat_id": "", "capture_broadcast": "", "pending_broadcast": ""},
            },
            upsert=True,
        )

    async def set_pending_broadcast(self, user_id: int, response: Any) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {
                "$set": {"pending_broadcast": response},
                "$unset": {"capture_chat_id": "", "capture_global": "", "capture_broadcast": ""},
            },
            upsert=True,
        )

    async def get_pending_broadcast(self, user_id: int) -> Any | None:
        document = await self.states_collection.find_one({"_id": user_id})
        return document.get("pending_broadcast") if document else None

    async def is_global_capture(self, user_id: int) -> bool:
        document = await self.states_collection.find_one({"_id": user_id})
        return bool(document and document.get("capture_global"))

    async def get_capture_group(self, user_id: int) -> int | None:
        document = await self.states_collection.find_one({"_id": user_id})
        return document.get("capture_chat_id") if document else None

    async def clear_capture_group(self, user_id: int) -> None:
        await self.states_collection.delete_one({"_id": user_id})
