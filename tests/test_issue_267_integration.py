"""Integration tests for #267: a ``.cache()`` read never outlives an ORM write.

Only ``save()``, ``update()``, ``merge()`` and ``delete()`` on an instance fire
the model signals the cache listens to, and the handlers were connected only by
``QueryCache.configure()``.  The atomic array helpers and the ``QuerySet`` bulk
writes fired no signal, and a process that never called ``configure()`` had no
handler at all, so a cached read kept serving the rows from before the write.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.cache import QueryCache
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.signals import post_delete, post_save, post_update
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

_DATABASE = "test_issue_267"


class I267Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i267_items")

    id: str | None = None
    label: str = ""
    tags: list[str] | None = None


@pytest.fixture(scope="module", autouse=True)
async def _connection() -> AsyncGenerator[None, None]:
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        _DATABASE,
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
def _never_configured() -> Iterator[None]:
    """Run as a process that never called ``QueryCache.configure()``.

    Other modules connect the handlers; this one starts with none and
    restores the registry afterwards so test order does not matter.
    """
    signals = (post_save, post_update, post_delete)
    saved = [{k: list(v) for k, v in sig._handlers.items()} for sig in signals]
    saved_flag = QueryCache._signals_connected
    saved_settings = (QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size)
    for sig in signals:
        sig._handlers.clear()
    QueryCache._signals_connected = False
    QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size = True, 60, 1000
    QueryCache.clear()
    try:
        yield
    finally:
        QueryCache.clear()
        for sig, handlers in zip(signals, saved, strict=True):
            sig._handlers.clear()
            sig._handlers.update(handlers)
        QueryCache._signals_connected = saved_flag
        QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size = saved_settings


@pytest.fixture(autouse=True)
async def _rows(_never_configured: None) -> None:
    client = await SurrealDBConnectionManager.get_client()
    await client.query("DELETE FROM i267_items;")
    await client.query("CREATE i267_items:a SET label = 'a', tags = ['x']; CREATE i267_items:b SET label = 'b', tags = ['x'];")


async def _cached() -> dict[str, tuple[str, list[str] | None]]:
    rows = await I267Item.objects().cache(ttl=60).exec()
    return {str(row.id): (row.label, row.tags) for row in rows}


class TestCachedReadSeesTheWrite:
    async def test_atomic_append(self) -> None:
        assert (await _cached())["a"] == ("a", ["x"])
        await I267Item.atomic_append("a", "tags", "y")
        assert (await _cached())["a"] == ("a", ["x", "y"])

    async def test_atomic_remove(self) -> None:
        assert (await _cached())["a"] == ("a", ["x"])
        await I267Item.atomic_remove("a", "tags", "x")
        assert (await _cached())["a"] == ("a", [])

    async def test_atomic_set_add(self) -> None:
        assert (await _cached())["a"] == ("a", ["x"])
        await I267Item.atomic_set_add("a", "tags", "z")
        assert (await _cached())["a"] == ("a", ["x", "z"])

    @pytest.mark.parametrize("atomic", [False, True], ids=["plain", "atomic"])
    async def test_bulk_update(self, atomic: bool) -> None:
        assert (await _cached())["b"] == ("b", ["x"])
        await I267Item.objects().filter(label="b").bulk_update({"label": "B"}, atomic=atomic)
        assert (await _cached())["b"] == ("B", ["x"])

    @pytest.mark.parametrize("atomic", [False, True], ids=["plain", "atomic"])
    async def test_bulk_delete(self, atomic: bool) -> None:
        assert sorted(await _cached()) == ["a", "b"]
        await I267Item.objects().filter(label="b").bulk_delete(atomic=atomic)
        assert sorted(await _cached()) == ["a"]

    async def test_save_without_configure(self) -> None:
        assert sorted(await _cached()) == ["a", "b"]
        await I267Item(id="c", label="c").save()
        assert sorted(await _cached()) == ["a", "b", "c"]
