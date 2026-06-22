from copy import deepcopy
from unittest.mock import patch

import pytest

from autoreply.repository import GroupRepository


class FakeCollection:
    def __init__(self, document):
        self.document = deepcopy(document)
        self.update_many_calls = []

    async def find_one_and_update(self, query, update, return_document):
        if (
            not self.document
            or self.document["_id"] != query["_id"]
            or not self.document.get("enabled")
        ):
            return None

        previous = deepcopy(self.document)
        self.document["next_index"] = self.document.get("next_index", 0) + 1
        return previous

    async def find_one(self, query, projection=None):
        if (
            self.document
            and self.document["_id"] == query["_id"]
            and ("enabled" not in query or self.document.get("enabled") == query["enabled"])
        ):
            return deepcopy(self.document)
        return None

    async def update_one(self, query, update, upsert=False):
        if not self.document:
            self.document = {"_id": query["_id"]}
        if "$set" in update:
            self.document.update(update["$set"])
        if "$setOnInsert" in update:
            for key, value in update["$setOnInsert"].items():
                self.document.setdefault(key, value)
        if "$addToSet" in update:
            for key, value in update["$addToSet"].items():
                values = self.document.setdefault(key, [])
                if value not in values:
                    values.append(value)
        if "$pull" in update:
            for key, value in update["$pull"].items():
                self.document[key] = [item for item in self.document.get(key, []) if item != value]
        if "$unset" in update:
            for key in update["$unset"]:
                self.document.pop(key, None)
        return type("Result", (), {"modified_count": 1})()

    async def update_many(self, query, update):
        self.update_many_calls.append((query, update))
        return type("Result", (), {"modified_count": 0})()


class FakeSettingsCollection:
    def __init__(self, responses=None, config=None):
        self.document = {
            "_id": "global_responses",
            "responses": responses or [],
            "next_index": 0,
        }
        self.config = {"_id": "global_config", **(config or {})}

    async def find_one_and_update(self, query, update, return_document):
        if not self.document["responses"]:
            return None
        previous = deepcopy(self.document)
        self.document["next_index"] = (self.document["next_index"] + 1) % len(
            self.document["responses"]
        )
        return previous

    async def find_one(self, query):
        return deepcopy(self.config if query["_id"] == "global_config" else self.document)

    async def update_one(self, query, update, upsert=False):
        target = self.config if query["_id"] == "global_config" else self.document
        if "$push" in update:
            target["responses"].append(update["$push"]["responses"])
        if "$set" in update:
            target.update(update["$set"])
        return type("Result", (), {"modified_count": 1})()


def repository_with(document, global_responses=None, global_config=None) -> GroupRepository:
    repository = GroupRepository.__new__(GroupRepository)
    repository.collection = FakeCollection(document)
    repository.settings_collection = FakeSettingsCollection(global_responses, global_config)
    return repository


@pytest.mark.asyncio
async def test_new_group_defaults_to_enabled_with_conservative_interactions() -> None:
    repository = repository_with(None)

    document = await repository.get(123)

    assert document["enabled"] is True
    assert document["reply_chance"] == 50
    assert document["cooldown_seconds"] == 10
    assert document["rate_limit_per_minute"] == 0


@pytest.mark.asyncio
async def test_ensure_group_immediately_creates_broadcast_target() -> None:
    repository = repository_with(None)

    await repository.ensure_group(-100123)

    assert repository.collection.document["_id"] == -100123
    assert repository.collection.document["responses"] == []
    assert repository.collection.document["config_overrides"] == []


@pytest.mark.asyncio
async def test_new_group_inherits_configurable_global_defaults() -> None:
    repository = repository_with(
        None,
        global_config={
            "enabled": False,
            "reply_chance": 25,
            "cooldown_seconds": 30,
            "rate_limit_per_minute": 5,
            "reactions_enabled": False,
            "reaction_chance": 0,
        },
    )

    document = await repository.get(123)

    assert document["enabled"] is False
    assert document["reply_chance"] == 25
    assert document["cooldown_seconds"] == 30
    assert document["rate_limit_per_minute"] == 5
    assert document["reactions_enabled"] is False
    assert document["reaction_chance"] == 0


@pytest.mark.asyncio
async def test_global_config_changes_immediately_affect_non_overridden_group() -> None:
    repository = repository_with(
        {"_id": 123, "enabled": True, "reply_chance": 100, "cooldown_seconds": 60},
        global_config={"reply_chance": 25, "cooldown_seconds": 30},
    )

    document = await repository.get(123)

    assert document["reply_chance"] == 25
    assert document["cooldown_seconds"] == 30


@pytest.mark.asyncio
async def test_local_override_survives_global_change_until_reset() -> None:
    repository = repository_with(
        {
            "_id": 123,
            "enabled": True,
            "reply_chance": 75,
            "config_overrides": ["reply_chance"],
        },
        global_config={"reply_chance": 25},
    )

    assert (await repository.get(123))["reply_chance"] == 75
    await repository.clear_local_config(123, "reply_chance")
    assert (await repository.get(123))["reply_chance"] == 25


@pytest.mark.asyncio
async def test_next_response_chooses_randomly() -> None:
    repository = repository_with(
        {
            "_id": 123,
            "enabled": True,
            "responses": ["one", "two", "three"],
            "next_index": 0,
        }
    )

    with patch("autoreply.repository.random.choice", return_value="three") as choice:
        assert await repository.next_response(123) == "three"
    choice.assert_called_once_with(["one", "two", "three"])


@pytest.mark.asyncio
async def test_next_response_returns_none_when_disabled() -> None:
    repository = repository_with(
        {"_id": 123, "enabled": False, "responses": ["one"], "next_index": 0}
    )

    assert await repository.next_response(123) is None


@pytest.mark.asyncio
async def test_next_response_returns_none_when_empty() -> None:
    repository = repository_with(
        {"_id": 123, "enabled": True, "responses": [], "next_index": 0}
    )

    assert await repository.next_response(123) is None


@pytest.mark.asyncio
async def test_next_response_uses_global_replies_when_group_has_none() -> None:
    repository = repository_with(
        {"_id": 123, "enabled": True, "responses": [], "next_index": 0},
        global_responses=["global one", "global two"],
    )

    with patch("autoreply.repository.random.choice", return_value="global two") as choice:
        assert await repository.next_response(123) == "global two"
    choice.assert_called_once_with(["global one", "global two"])


@pytest.mark.asyncio
async def test_next_response_uses_global_replies_before_group_document_exists() -> None:
    repository = repository_with(None, global_responses=["global"])

    assert await repository.next_response(123) == "global"


@pytest.mark.asyncio
async def test_adding_global_reply_clears_stale_group_exclusions() -> None:
    response = {"kind": "message", "message_id": 42}
    repository = repository_with(None)

    assert await repository.add_global_response(response) == "added"
    assert await repository.get_global_responses() == [response]
    assert repository.collection.update_many_calls == [
        (
            {"excluded_global_responses": response},
            {"$pull": {"excluded_global_responses": response}},
        )
    ]


@pytest.mark.asyncio
async def test_next_response_combines_local_and_global_replies() -> None:
    repository = repository_with(
        {"_id": 123, "enabled": True, "responses": ["local"], "next_index": 0},
        global_responses=["global"],
    )

    with patch("autoreply.repository.random.choice", return_value="global") as choice:
        assert await repository.next_response(123) == "global"
    choice.assert_called_once_with(["local", "global"])


@pytest.mark.asyncio
async def test_reply_chance_defaults_to_50_for_enabled_existing_group() -> None:
    repository = repository_with(
        {"_id": 123, "enabled": True, "responses": ["local"], "next_index": 0}
    )

    assert await repository.reply_chance(123) == 50


@pytest.mark.asyncio
async def test_reply_chance_returns_none_for_disabled_group() -> None:
    repository = repository_with(
        {"_id": 123, "enabled": False, "responses": ["local"], "next_index": 0}
    )

    assert await repository.reply_chance(123) is None


@pytest.mark.asyncio
async def test_remove_reaction_persists_default_reaction_list_without_invalid_value() -> None:
    repository = repository_with(None)

    assert await repository.remove_reaction(123, "👀")
    document = await repository.get(123)

    assert "👀" not in document["reactions"]


@pytest.mark.asyncio
async def test_stats_count_private_users_all_users_and_groups() -> None:
    class CountingCollection:
        def __init__(self, counts):
            self.counts = counts

        async def count_documents(self, query):
            return self.counts.get(str(query), 0)

    repository = GroupRepository.__new__(GroupRepository)
    repository.users_collection = CountingCollection(
        {
            str({"private_interacted": True}): 4,
            str({}): 10,
        }
    )
    repository.collection = CountingCollection({str({}): 7})

    assert await repository.stats() == {
        "private_users": 4,
        "users": 10,
        "groups": 7,
    }


@pytest.mark.asyncio
async def test_record_private_user_does_not_conflict_with_insert_defaults() -> None:
    class RecordingCollection:
        def __init__(self):
            self.calls = []

        async def update_one(self, query, update, upsert=False):
            self.calls.append((query, update, upsert))

    repository = GroupRepository.__new__(GroupRepository)
    repository.users_collection = RecordingCollection()

    await repository.record_user(123, private=True)
    await repository.record_user(456, private=False)

    assert repository.users_collection.calls == [
        (
            {"_id": 123},
            {"$set": {"private_interacted": True}},
            True,
        ),
        (
            {"_id": 456},
            {"$setOnInsert": {"private_interacted": False}},
            True,
        ),
    ]
