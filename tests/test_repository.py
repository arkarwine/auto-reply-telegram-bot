from copy import deepcopy

import pytest

from autoreply.repository import GroupRepository


class FakeCollection:
    def __init__(self, document):
        self.document = deepcopy(document)

    async def find_one_and_update(self, query, update, return_document):
        if (
            not self.document
            or self.document["_id"] != query["_id"]
            or not self.document.get("enabled")
            or not self.document.get("responses")
        ):
            return None

        previous = deepcopy(self.document)
        self.document["next_index"] = (self.document.get("next_index", 0) + 1) % len(
            self.document["responses"]
        )
        return previous


def repository_with(document) -> GroupRepository:
    repository = GroupRepository.__new__(GroupRepository)
    repository.collection = FakeCollection(document)
    return repository


@pytest.mark.asyncio
async def test_next_response_rotates_in_order() -> None:
    repository = repository_with(
        {
            "_id": 123,
            "enabled": True,
            "responses": ["one", "two", "three"],
            "next_index": 0,
        }
    )

    assert await repository.next_response(123) == "one"
    assert await repository.next_response(123) == "two"
    assert await repository.next_response(123) == "three"
    assert await repository.next_response(123) == "one"


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
