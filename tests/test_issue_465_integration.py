"""
Integration tests for #465: a ``.cache()`` read of a model mapped to an edge
table sees the rows written by ``relate()`` / ``remove_relation()``.

Before the fix the relation writes fired no signal and dropped nothing, so the
cached read kept returning the rows from before the write until its TTL ran out.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.cache import QueryCache
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_465"

pytestmark = pytest.mark.integration

_EDGE = "i465_int_follows"


class I465IntUser(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i465_int_users")
    id: str | None = None
    name: str = ""


class I465IntFollows(BaseSurrealModel):
    """Mapped to the edge table ``relate()`` writes."""

    model_config = SurrealConfigDict(table_name=_EDGE)
    id: str | None = None
    since: str = ""


@pytest.fixture(autouse=True)
async def i465_users() -> AsyncGenerator[tuple[I465IntUser, I465IntUser], None]:
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        f"REMOVE TABLE IF EXISTS i465_int_users; REMOVE TABLE IF EXISTS {_EDGE}; DEFINE TABLE {_EDGE} TYPE RELATION;"
    )
    QueryCache.clear()
    alice = I465IntUser(id="alice", name="Alice")
    bob = I465IntUser(id="bob", name="Bob")
    await alice.save()
    await bob.save()
    try:
        yield alice, bob
    finally:
        QueryCache.clear()
        await client.query(f"REMOVE TABLE IF EXISTS i465_int_users; REMOVE TABLE IF EXISTS {_EDGE};")
        await SurrealDBConnectionManager.close_connection()
        await SurrealDBConnectionManager.unset_connection()


async def _cached_edges() -> list[I465IntFollows]:
    return await I465IntFollows.objects().cache(ttl=300).exec()


async def test_cached_read_sees_relate(i465_users: tuple[I465IntUser, I465IntUser]) -> None:
    alice, bob = i465_users
    assert await _cached_edges() == []

    await alice.relate(_EDGE, bob, since="2026")

    edges = await _cached_edges()
    assert [edge.since for edge in edges] == ["2026"]


async def test_cached_read_sees_remove_relation(i465_users: tuple[I465IntUser, I465IntUser]) -> None:
    alice, bob = i465_users
    await alice.relate(_EDGE, bob)
    assert len(await _cached_edges()) == 1

    await alice.remove_relation(_EDGE, bob)

    assert await _cached_edges() == []


async def test_cached_read_after_commit_sees_relate_tx(i465_users: tuple[I465IntUser, I465IntUser]) -> None:
    alice, bob = i465_users
    assert await _cached_edges() == []

    async with await SurrealDBConnectionManager.transaction() as tx:
        await alice.relate(_EDGE, bob, tx=tx, since="tx")

    edges = await _cached_edges()
    assert [edge.since for edge in edges] == ["tx"]
