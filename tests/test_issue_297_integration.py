"""Integration tests for issue #297 — QueryCache results follow the connection.

Two named connections point at two databases holding the same table and the
same record ID with different contents.  ``.cache()`` under each connection
must return that database's rows.  A write through one connection invalidates
the table's entries for every connection (issue #338).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from surreal_orm import SurrealDBConnectionManager
from surreal_orm.cache import QueryCache
from surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

_CONNECTIONS = {"i297_conn_a": "test_i297_a", "i297_conn_b": "test_i297_b"}


class I297Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i297_items")
    id: str | None = None
    label: str = ""


async def _labels(connection: str) -> list[str]:
    async with SurrealDBConnectionManager.using(connection):
        rows = await I297Item.objects().cache(ttl=60).exec()
    return sorted(row.label for row in rows)


@pytest.fixture(scope="module", autouse=True)
async def _connections() -> AsyncGenerator[None, None]:
    for name, database in _CONNECTIONS.items():
        SurrealDBConnectionManager.add_connection(
            name,
            url=SURREALDB_URL,
            user=SURREALDB_USER,
            password=SURREALDB_PASS,
            namespace=SURREALDB_NAMESPACE,
            database=database,
        )
    yield
    for name in _CONNECTIONS:
        await SurrealDBConnectionManager.remove_connection(name)


@pytest.fixture(autouse=True)
async def _data() -> AsyncGenerator[None, None]:
    QueryCache.configure(enabled=True, default_ttl=60)
    for name, label in (("i297_conn_a", "from-A"), ("i297_conn_b", "from-B")):
        client = await SurrealDBConnectionManager.get_client(name)
        await client.query("DELETE FROM i297_items;")
        await client.query("CREATE i297_items:r1 SET label = $label;", {"label": label})
    QueryCache.clear()
    yield
    QueryCache.clear()


async def test_cache_returns_each_connections_own_rows() -> None:
    assert await _labels("i297_conn_a") == ["from-A"]
    assert await _labels("i297_conn_b") == ["from-B"]
    # Second round is served from the cache: still one entry per database.
    assert await _labels("i297_conn_a") == ["from-A"]
    assert await _labels("i297_conn_b") == ["from-B"]
    assert QueryCache.stats()["entries"] == 2


async def test_save_invalidates_every_connection_reads_stay_separate() -> None:
    assert await _labels("i297_conn_a") == ["from-A"]
    assert await _labels("i297_conn_b") == ["from-B"]
    assert QueryCache.stats()["entries"] == 2

    async with SurrealDBConnectionManager.using("i297_conn_a"):
        await I297Item(id="r2", label="from-A-2").save()

    # Invalidation fails safe (issue #338): both entries go, but each database
    # still answers with its own rows.
    assert QueryCache.stats()["entries"] == 0
    assert await _labels("i297_conn_a") == ["from-A", "from-A-2"]
    assert await _labels("i297_conn_b") == ["from-B"]
