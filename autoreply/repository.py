from typing import Any

from pymongo import AsyncMongoClient, ReturnDocument


MAX_RESPONSES = 100
MAX_REACTIONS = 20
DEFAULT_REACTIONS = ["👍", "❤️", "😂", "🎉", "👀"]
DEFAULT_REACTION_CHANCE = 25
DEFAULT_REPLY_CHANCE = 100


class GroupRepository:
    def __init__(self, mongodb_uri: str, database_name: str) -> None:
        self.client = AsyncMongoClient(mongodb_uri)
        self.collection = self.client[database_name]["groups"]
        self.settings_collection = self.client[database_name]["bot_settings"]
        self.states_collection = self.client[database_name]["user_states"]

    async def ping(self) -> None:
        await self.client.admin.command("ping")

    async def close(self) -> None:
        await self.client.close()

    async def get(self, chat_id: int) -> dict[str, Any]:
        document = await self.collection.find_one({"_id": chat_id})
        defaults = {
            "_id": chat_id,
            "enabled": False,
            "responses": [],
            "next_index": 0,
            "reply_chance": DEFAULT_REPLY_CHANCE,
            "reactions_enabled": True,
            "reaction_chance": DEFAULT_REACTION_CHANCE,
            "reactions": list(DEFAULT_REACTIONS),
        }
        return defaults | document if document else defaults

    async def set_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.collection.update_one(
            {"_id": chat_id},
            {
                "$set": {"enabled": enabled},
                "$setOnInsert": {
                    "responses": [],
                    "next_index": 0,
                    "reply_chance": DEFAULT_REPLY_CHANCE,
                    "reactions_enabled": True,
                    "reaction_chance": DEFAULT_REACTION_CHANCE,
                    "reactions": DEFAULT_REACTIONS,
                },
            },
            upsert=True,
        )

    async def add_response(self, chat_id: int, response: Any) -> str:
        document = await self.get(chat_id)
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
                    "enabled": False,
                    "next_index": 0,
                    "reply_chance": DEFAULT_REPLY_CHANCE,
                    "reactions_enabled": True,
                    "reaction_chance": DEFAULT_REACTION_CHANCE,
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
        document = await self.collection.find_one_and_update(
            {"_id": chat_id, "enabled": True},
            {"$inc": {"next_index": 1}},
            return_document=ReturnDocument.BEFORE,
        )
        if not document:
            return None

        responses = document.get("responses", []) + global_responses
        if not responses:
            return None
        return responses[document.get("next_index", 0) % len(responses)]

    async def reply_chance(self, chat_id: int) -> int | None:
        document = await self.collection.find_one(
            {"_id": chat_id, "enabled": True},
            {"reply_chance": 1},
        )
        return document.get("reply_chance", DEFAULT_REPLY_CHANCE) if document else None

    async def set_reply_chance(self, chat_id: int, chance: int) -> None:
        await self.collection.update_one(
            {"_id": chat_id},
            {
                "$set": {"reply_chance": chance},
                "$setOnInsert": {
                    "enabled": False,
                    "responses": [],
                    "next_index": 0,
                    "reactions_enabled": True,
                    "reaction_chance": DEFAULT_REACTION_CHANCE,
                    "reactions": DEFAULT_REACTIONS,
                },
            },
            upsert=True,
        )

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
        await self.collection.update_one(
            {"_id": chat_id},
            {
                "$set": {"reactions_enabled": enabled},
                "$setOnInsert": {
                    "enabled": False,
                    "responses": [],
                    "next_index": 0,
                    "reply_chance": DEFAULT_REPLY_CHANCE,
                    "reaction_chance": DEFAULT_REACTION_CHANCE,
                    "reactions": DEFAULT_REACTIONS,
                },
            },
            upsert=True,
        )

    async def set_reaction_chance(self, chat_id: int, chance: int) -> None:
        await self.collection.update_one(
            {"_id": chat_id},
            {
                "$set": {"reaction_chance": chance},
                "$setOnInsert": {
                    "enabled": False,
                    "responses": [],
                    "next_index": 0,
                    "reply_chance": DEFAULT_REPLY_CHANCE,
                    "reactions_enabled": True,
                    "reactions": DEFAULT_REACTIONS,
                },
            },
            upsert=True,
        )

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
                    "enabled": False,
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
        result = await self.collection.update_one(
            {"_id": chat_id, "reactions": reaction},
            {"$pull": {"reactions": reaction}},
        )
        return result.modified_count > 0

    async def reaction_settings(self, chat_id: int) -> tuple[int, list[str]] | None:
        document = await self.collection.find_one(
            {"_id": chat_id, "enabled": True, "reactions_enabled": {"$ne": False}},
            {"reaction_chance": 1, "reactions": 1},
        )
        if not document:
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

    async def set_capture_group(self, user_id: int, chat_id: int) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {"$set": {"capture_chat_id": chat_id}, "$unset": {"capture_global": ""}},
            upsert=True,
        )

    async def set_global_capture(self, user_id: int) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {"$set": {"capture_global": True}, "$unset": {"capture_chat_id": ""}},
            upsert=True,
        )

    async def is_global_capture(self, user_id: int) -> bool:
        document = await self.states_collection.find_one({"_id": user_id})
        return bool(document and document.get("capture_global"))

    async def get_capture_group(self, user_id: int) -> int | None:
        document = await self.states_collection.find_one({"_id": user_id})
        return document.get("capture_chat_id") if document else None

    async def clear_capture_group(self, user_id: int) -> None:
        await self.states_collection.delete_one({"_id": user_id})
