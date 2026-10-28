"""Integration tests for issue #358 (v2 line, SurrealDB 2.x) — a write never leaves a stale ``.cache()`` result.

One database is reached through two spellings of its URL (two cache buckets),
and a model routed to another database is saved inside a transaction opened on
the first: in both cases the cached read of the table must be dropped.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.cache import QueryCache
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

_DB = "test_i358_v2"
_OTHER_DB = "test_i358_v2_other"


class I358Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i358_items")
    id: str | None = None
    label: str = ""


class I358RoutedItem(BaseSurrealModel):
    """Same table, but routed to a connection on another database."""

    model_config = SurrealConfigDict(table_name="i358_items", connection="i358_other")
    id: str | None = None
    label: str = ""


async def _labels(connection: str) -> list[str]:
    async with SurrealDBConnectionManager.using(connection):
        rows = await I358Item.objects().cache(ttl=60).exec()
    return sorted(row.label for row in rows)


@pytest.fixture(autouse=True)
async def _connections() -> AsyncGenerator[None, None]:
    # One database, two spellings of its URL: two cache buckets.
    connections = {
        "i358_plain": (SURREALDB_URL.rstrip("/"), _DB),
        "i358_slash": (SURREALDB_URL.rstrip("/") + "/", _DB),
        "i358_other": (SURREALDB_URL, _OTHER_DB),
    }
    for name, (url, database) in connections.items():
        SurrealDBConnectionManager.add_connection(
            name,
            url=url,
            user=SURREALDB_USER,
            password=SURREALDB_PASS,
            namespace=SURREALDB_NAMESPACE,
            database=database,
        )
    for name in ("i358_plain", "i358_other"):
        client = await SurrealDBConnectionManager.get_client(name)
        await client.query("DELETE FROM i358_items;")
    client = await SurrealDBConnectionManager.get_client("i358_plain")
    await client.query("CREATE i358_items:r1 SET label = 'one';")
    QueryCache.configure(enabled=True, default_ttl=60)
    QueryCache.clear()
    yield
    QueryCache.clear()
    for name in connections:
        await SurrealDBConnectionManager.remove_connection(name)


async def test_save_through_one_spelling_invalidates_the_other() -> None:
    assert await _labels("i358_slash") == ["one"]
    async with SurrealDBConnectionManager.using("i358_plain"):
        await I358Item(id="r2", label="two").save()
    assert await _labels("i358_slash") == ["one", "two"]


async def test_save_in_an_http_transaction_invalidates_whatever_the_routing() -> None:
    assert await _labels("i358_plain") == ["one"]
    # The model routes to i358_other; the HTTP transaction runs on i358_plain.
    client = await SurrealDBConnectionManager.get_client("i358_plain")
    async with client.transaction() as tx:
        await I358RoutedItem(id="r3", label="three").save(tx=tx)
    assert await _labels("i358_plain") == ["one", "three"]
    # The row went to the transaction's database, not the routed one.
    assert await _labels("i358_other") == []
