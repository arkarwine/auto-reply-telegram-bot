from typing import Any
import random

from pymongo import AsyncMongoClient


MAX_REACTIONS = 20
DEFAULT_REACTIONS = ["👍", "❤️", "😂", "🎉", "👀"]
DEFAULT_REACTION_CHANCE = 25
DEFAULT_REPLY_CHANCE = 50
DEFAULT_COOLDOWN_SECONDS = 10
DEFAULT_RATE_LIMIT_PER_MINUTE = 0
DEFAULT_REPLY_MODE = "random"
REPLY_MODES = ("random", "keyword")
GLOBAL_CONFIG_KEYS = (
    "reply_mode",
    "enabled",
    "reply_chance",
    "cooldown_seconds",
    "rate_limit_per_minute",
    "reactions_enabled",
    "reaction_chance",
    "reactions",
)


class GroupRepository:
    def __init__(self, mongodb_uri: str, database_name: str) -> None:
        self.client = AsyncMongoClient(mongodb_uri)
        self.collection = self.client[database_name]["groups"]
        self.settings_collection = self.client[database_name]["bot_settings"]
        self.states_collection = self.client[database_name]["user_states"]
        self.users_collection = self.client[database_name]["users"]

    async def ping(self) -> None:
        await self.client.admin.command("ping")

    async def close(self) -> None:
        await self.client.close()

    async def get_global_config(self) -> dict[str, Any]:
        document = await self.settings_collection.find_one({"_id": "global_config"})
        defaults = {
            "reply_mode": DEFAULT_REPLY_MODE,
            "enabled": True,
            "reply_chance": DEFAULT_REPLY_CHANCE,
            "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
            "rate_limit_per_minute": DEFAULT_RATE_LIMIT_PER_MINUTE,
            "reactions_enabled": True,
            "reaction_chance": DEFAULT_REACTION_CHANCE,
            "reactions": list(DEFAULT_REACTIONS),
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
                    "keyword_responses": [],
                    "keyword_reactions": [],
                    "global_replies_enabled": True,
                    "global_reactions_enabled": True,
                    "excluded_global_responses": [],
                    "reactions": DEFAULT_REACTIONS,
                    "config_overrides": [],
                }
            },
            upsert=True,
        )

    async def get(self, chat_id: int) -> dict[str, Any]:
        document = await self.collection.find_one({"_id": chat_id})
        global_config = await self.get_global_config()
        defaults = {
            "_id": chat_id,
            "reply_mode": global_config["reply_mode"],
            "enabled": global_config["enabled"],
            "responses": [],
            "keyword_responses": [],
            "keyword_reactions": [],
            "next_index": 0,
            "reply_chance": global_config["reply_chance"],
            "cooldown_seconds": global_config["cooldown_seconds"],
            "rate_limit_per_minute": global_config["rate_limit_per_minute"],
            "global_replies_enabled": True,
            "global_reactions_enabled": True,
            "excluded_global_responses": [],
            "reactions_enabled": global_config["reactions_enabled"],
            "reaction_chance": global_config["reaction_chance"],
            "reactions": list(global_config.get("reactions", DEFAULT_REACTIONS)),
            "config_overrides": [],
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

    async def set_reply_mode(self, chat_id: int, mode: str) -> None:
        if mode not in REPLY_MODES:
            raise ValueError(f"Unsupported reply mode: {mode}")
        await self.set_local_config(chat_id, "reply_mode", mode)

    async def group_ids(self) -> list[int]:
        return [document["_id"] async for document in self.collection.find({}, {"_id": 1})]

    async def user_ids(self) -> list[int]:
        return [document["_id"] async for document in self.users_collection.find({}, {"_id": 1})]

    async def record_user(self, user_id: int, private: bool = False) -> None:
        update: dict[str, Any] = (
            {"$set": {"private_interacted": True}}
            if private
            else {"$setOnInsert": {"private_interacted": False}}
        )
        await self.users_collection.update_one({"_id": user_id}, update, upsert=True)

    async def stats(self) -> dict[str, int]:
        return {
            "private_users": await self.users_collection.count_documents(
                {"private_interacted": True}
            ),
            "users": await self.users_collection.count_documents({}),
            "groups": await self.collection.count_documents({}),
        }

    async def add_response(self, chat_id: int, response: Any) -> str:
        document = await self.get(chat_id)
        global_config = await self.get_global_config()
        responses = document["responses"]
        if response in responses:
            return "duplicate"

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
                    "global_reactions_enabled": True,
                    "excluded_global_responses": [],
                    "reactions_enabled": global_config["reactions_enabled"],
                    "reaction_chance": global_config["reaction_chance"],
                    "reactions": DEFAULT_REACTIONS,
                },
            },
            upsert=True,
        )
        return "added"

    async def add_keyword_response(self, chat_id: int, keywords: list[str], response: Any) -> str:
        keywords = normalize_keywords(keywords)
        if not keywords:
            return "missing_keyword"
        document = await self.get(chat_id)
        entry = {"keywords": keywords, "response": response}
        responses = list(document.get("keyword_responses", []))
        if entry in responses:
            return "duplicate"
        responses.append(entry)
        await self.collection.update_one(
            {"_id": chat_id},
            {
                "$set": {"keyword_responses": responses},
                "$setOnInsert": {
                    "enabled": True,
                    "responses": [],
                    "keyword_reactions": [],
                    "global_replies_enabled": True,
                    "global_reactions_enabled": True,
                    "excluded_global_responses": [],
                    "reactions": DEFAULT_REACTIONS,
                    "config_overrides": [],
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

    async def remove_keyword_response(self, chat_id: int, one_based_index: int) -> Any | None:
        document = await self.get(chat_id)
        responses = list(document.get("keyword_responses", []))
        if one_based_index < 1 or one_based_index > len(responses):
            return None
        removed = responses.pop(one_based_index - 1)
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"keyword_responses": responses}},
            upsert=True,
        )
        return removed

    async def clear_all_responses(self, chat_id: int) -> int:
        document = await self.get(chat_id)
        count = len(document.get("responses", [])) + len(document.get("keyword_responses", []))
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"responses": [], "keyword_responses": [], "next_index": 0}},
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

    async def keyword_response(self, chat_id: int, text: str) -> Any | None:
        document = await self.get(chat_id)
        if not document["enabled"] or document.get("reply_mode") != "keyword":
            return None
        global_responses = []
        if document.get("global_replies_enabled", True):
            global_responses = await self.get_global_keyword_responses()
        matched = [
            entry["response"]
            for entry in [*document.get("keyword_responses", []), *global_responses]
            if keyword_matches(text, entry.get("keywords", []))
        ]
        return random.choice(matched) if matched else None

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

    async def toggle_global_reactions(self, chat_id: int) -> bool:
        document = await self.get(chat_id)
        enabled = not document.get("global_reactions_enabled", True)
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"global_reactions_enabled": enabled}},
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

    async def get_global_keyword_responses(self) -> list[Any]:
        document = await self.settings_collection.find_one({"_id": "global_keyword_responses"})
        return document.get("responses", []) if document else []

    async def add_global_response(self, response: Any) -> str:
        responses = await self.get_global_responses()
        if response in responses:
            return "duplicate"
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

    async def add_global_keyword_response(self, keywords: list[str], response: Any) -> str:
        keywords = normalize_keywords(keywords)
        if not keywords:
            return "missing_keyword"
        entry = {"keywords": keywords, "response": response}
        responses = await self.get_global_keyword_responses()
        if entry in responses:
            return "duplicate"
        await self.settings_collection.update_one(
            {"_id": "global_keyword_responses"},
            {"$push": {"responses": entry}},
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

    async def remove_global_keyword_response(self, one_based_index: int) -> Any | None:
        responses = await self.get_global_keyword_responses()
        if one_based_index < 1 or one_based_index > len(responses):
            return None
        removed = responses.pop(one_based_index - 1)
        await self.settings_collection.update_one(
            {"_id": "global_keyword_responses"},
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

    async def clear_global_keyword_responses(self) -> int:
        responses = await self.get_global_keyword_responses()
        await self.settings_collection.update_one(
            {"_id": "global_keyword_responses"},
            {"$set": {"responses": []}},
            upsert=True,
        )
        return len(responses)

    async def add_global_reaction(self, reaction: str) -> str:
        reaction = reaction.strip()
        if not reaction:
            return "missing"
        config = await self.get_global_config()
        reactions = list(config.get("reactions", DEFAULT_REACTIONS))
        if reaction in reactions:
            return "duplicate"
        if len(reactions) >= MAX_REACTIONS:
            return "full"
        reactions.append(reaction)
        await self.set_global_config("reactions", reactions)
        return "added"

    async def get_global_keyword_reactions(self) -> list[dict[str, Any]]:
        document = await self.settings_collection.find_one({"_id": "global_keyword_reactions"})
        return document.get("reactions", []) if document else []

    async def add_global_keyword_reaction(self, keywords: list[str], reaction: str) -> str:
        keywords = normalize_keywords(keywords)
        reaction = reaction.strip()
        if not keywords or not reaction:
            return "missing_keyword"
        entry = {"keywords": keywords, "reaction": reaction}
        reactions = await self.get_global_keyword_reactions()
        if entry in reactions:
            return "duplicate"
        reactions.append(entry)
        await self.settings_collection.update_one(
            {"_id": "global_keyword_reactions"},
            {"$set": {"reactions": reactions}},
            upsert=True,
        )
        return "added"

    async def remove_global_reaction(self, one_based_index: int) -> str | None:
        reactions = list((await self.get_global_config()).get("reactions", DEFAULT_REACTIONS))
        if one_based_index < 1 or one_based_index > len(reactions):
            return None
        removed = reactions.pop(one_based_index - 1)
        await self.set_global_config("reactions", reactions)
        return removed

    async def clear_global_reactions(self) -> int:
        reactions = list((await self.get_global_config()).get("reactions", DEFAULT_REACTIONS))
        await self.set_global_config("reactions", [])
        return len(reactions)

    async def clear_global_keyword_reactions(self) -> int:
        reactions = await self.get_global_keyword_reactions()
        await self.settings_collection.update_one(
            {"_id": "global_keyword_reactions"},
            {"$set": {"reactions": []}},
            upsert=True,
        )
        return len(reactions)

    async def set_reactions_enabled(self, chat_id: int, enabled: bool) -> None:
        await self.set_local_config(chat_id, "reactions_enabled", enabled)

    async def set_reaction_chance(self, chat_id: int, chance: int) -> None:
        await self.set_local_config(chat_id, "reaction_chance", chance)

    async def add_reaction(self, chat_id: int, reaction: str) -> str:
        document = await self.get(chat_id)
        reactions = (
            list(document.get("reactions", []))
            if "reactions" in document.get("config_overrides", [])
            else []
        )
        if reaction in reactions:
            return "duplicate"
        if len(reactions) >= MAX_REACTIONS:
            return "full"

        reactions.append(reaction)
        await self.set_local_config(chat_id, "reactions", reactions)
        return "added"

    async def add_keyword_reaction(self, chat_id: int, keywords: list[str], reaction: str) -> str:
        keywords = normalize_keywords(keywords)
        reaction = reaction.strip()
        if not keywords or not reaction:
            return "missing_keyword"
        document = await self.get(chat_id)
        entry = {"keywords": keywords, "reaction": reaction}
        reactions = list(document.get("keyword_reactions", []))
        if entry in reactions:
            return "duplicate"
        reactions.append(entry)
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"keyword_reactions": reactions}},
            upsert=True,
        )
        return "added"

    async def clear_reactions(self, chat_id: int) -> int:
        document = await self.get(chat_id)
        count = (
            len(document.get("reactions", []))
            if "reactions" in document.get("config_overrides", [])
            else 0
        )
        await self.set_local_config(chat_id, "reactions", [])
        return count

    async def clear_keyword_reactions(self, chat_id: int) -> int:
        document = await self.get(chat_id)
        count = len(document.get("keyword_reactions", []))
        await self.collection.update_one(
            {"_id": chat_id},
            {"$set": {"keyword_reactions": []}},
            upsert=True,
        )
        return count

    async def remove_reaction(self, chat_id: int, reaction: str) -> bool:
        document = await self.get(chat_id)
        local_reactions = (
            list(document.get("reactions", []))
            if "reactions" in document.get("config_overrides", [])
            else []
        )
        if reaction in local_reactions:
            local_reactions.remove(reaction)
            await self.set_local_config(chat_id, "reactions", local_reactions)
            return True

        global_reactions = list((await self.get_global_config()).get("reactions", DEFAULT_REACTIONS))
        if reaction in global_reactions:
            global_reactions.remove(reaction)
            await self.set_global_config("reactions", global_reactions)
            return True
        return False

    async def reaction_settings(self, chat_id: int) -> tuple[int, list[str]] | None:
        document = await self.get(chat_id)
        if (
            not document["enabled"]
            or document.get("reply_mode") == "keyword"
            or not document.get("reactions_enabled", True)
        ):
            return None
        local_reactions = (
            list(document.get("reactions", []))
            if "reactions" in document.get("config_overrides", [])
            else []
        )
        global_reactions = (
            list((await self.get_global_config()).get("reactions", DEFAULT_REACTIONS))
            if document.get("global_reactions_enabled", True)
            else []
        )
        reactions = list(dict.fromkeys([*local_reactions, *global_reactions]))
        return (
            document.get("reaction_chance", DEFAULT_REACTION_CHANCE),
            reactions,
        )

    async def keyword_reaction(self, chat_id: int, text: str) -> str | None:
        document = await self.get(chat_id)
        if not document["enabled"] or document.get("reply_mode") != "keyword":
            return None
        global_reactions = (
            await self.get_global_keyword_reactions()
            if document.get("global_reactions_enabled", True)
            else []
        )
        matched = [
            entry["reaction"]
            for entry in [*document.get("keyword_reactions", []), *global_reactions]
            if keyword_matches(text, entry.get("keywords", []))
        ]
        return random.choice(matched) if matched else None

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

    async def set_capture_group(self, user_id: int, chat_id: int, keywords: list[str] | None = None) -> None:
        update: dict[str, Any] = {"capture_chat_id": chat_id}
        if keywords:
            update["capture_keywords"] = normalize_keywords(keywords)
        await self.states_collection.update_one(
            {"_id": user_id},
            {
                "$set": update,
                "$unset": {
                    "capture_global": "",
                    "capture_broadcast": "",
                    "pending_broadcast": "",
                    "capture_keyword_prompt": "",
                    "capture_reaction_prompt": "",
                    "capture_global_keyword_prompt": "",
                    "capture_global_reaction_prompt": "",
                },
            },
            upsert=True,
        )

    async def set_keyword_prompt(self, user_id: int, chat_id: int, reaction: bool = False) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {
                "$set": {
                    "capture_chat_id": chat_id,
                    "capture_keyword_prompt": True,
                    "capture_reaction_prompt": reaction,
                },
                "$unset": {"capture_global": "", "pending_broadcast": "", "capture_keywords": ""},
            },
            upsert=True,
        )

    async def set_capture_reaction(self, user_id: int) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {"$set": {"capture_reaction": True}},
            upsert=True,
        )

    async def set_reaction_capture(self, user_id: int, chat_id: int | None = None, global_: bool = False) -> None:
        update: dict[str, Any] = {"capture_reaction": True}
        if chat_id is not None:
            update["capture_chat_id"] = chat_id
        if global_:
            update["capture_global_reaction"] = True
        await self.states_collection.update_one(
            {"_id": user_id},
            {
                "$set": update,
                "$unset": {
                    "capture_global": "",
                    "capture_keyword_prompt": "",
                    "capture_global_keyword_prompt": "",
                    "capture_global_reaction_prompt": "",
                },
            },
            upsert=True,
        )

    async def set_global_keyword_prompt(self, user_id: int, reaction: bool = False) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {
                "$set": {
                    "capture_global_keyword_prompt": True,
                    "capture_global_reaction_prompt": reaction,
                },
                "$unset": {
                    "capture_chat_id": "",
                    "capture_global": "",
                    "capture_global_reaction": "",
                    "capture_keywords": "",
                },
            },
            upsert=True,
        )

    async def set_global_capture(self, user_id: int) -> None:
        await self.states_collection.update_one(
            {"_id": user_id},
            {
                "$set": {"capture_global": True},
                "$unset": {
                    "capture_chat_id": "",
                    "capture_broadcast": "",
                    "pending_broadcast": "",
                    "capture_global_keyword_prompt": "",
                    "capture_global_reaction_prompt": "",
                },
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

    async def get_capture_state(self, user_id: int) -> dict[str, Any]:
        return await self.states_collection.find_one({"_id": user_id}) or {}

    async def clear_capture_group(self, user_id: int) -> None:
        await self.states_collection.delete_one({"_id": user_id})


def normalize_keywords(keywords: list[str]) -> list[str]:
    normalized = []
    for keyword in keywords:
        value = " ".join(keyword.casefold().strip().split())
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def split_keywords(value: str) -> list[str]:
    return normalize_keywords(value.replace("\n", ",").split(","))


def keyword_matches(text: str, keywords: list[str]) -> bool:
    haystack = " ".join(text.casefold().split())
    return bool(haystack and any(keyword in haystack for keyword in normalize_keywords(keywords)))
