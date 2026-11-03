"""Unit tests for #463: every ORM write invalidates ``QueryCache``, without ``configure()``.

Only ``save()``, ``update()``, ``merge()`` and ``delete()`` on an instance fire
the ``post_*`` signals the cache listens to, and those handlers were connected
only by ``QueryCache.configure()``.  The atomic array helpers and the
``QuerySet`` bulk writes fire no signal, and a process that never called
``configure()`` had no handler at all, so a ``.cache()`` read kept serving the
rows from before the write until its TTL ran out.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.surreal_orm.cache import QueryCache
from src.surreal_orm.connection_manager import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.signals import post_delete, post_save, post_update
from surreal_sdk.exceptions import QueryError
from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus


class U463Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u463_items")

    id: str | None = None
    name: str = ""
    age: int = 0
    tags: list[str] | None = None


def _ok(records: list[dict[str, Any]] | None = None) -> QueryResponse:
    """One statement SurrealDB accepted."""
    return QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=records or [])])


def _err() -> QueryResponse:
    """One statement SurrealDB rejected, inside a successful RPC."""
    return QueryResponse(results=[QueryResult(status=ResponseStatus.ERR, result="rejected")])


def _client(response: QueryResponse | None = None) -> AsyncMock:
    client = AsyncMock()
    client.query = AsyncMock(
        return_value=response if response is not None else _ok([{"id": "u463_items:r1", "name": "n", "count": 1}])
    )
    client.delete = AsyncMock(return_value=None)
    return client


class _Transaction:
    """Stands in for ``HTTPTransaction``: an async context manager with ``query``.

    With *fail_commit*, leaving the block raises, as a rejected commit does.
    """

    def __init__(self, *, fail_commit: bool = False) -> None:
        self.query = AsyncMock(return_value=_ok([{"id": "u463_items:r1", "name": "n"}]))
        self._fail_commit = fail_commit

    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._fail_commit:
            raise QueryError("commit rejected")


@pytest.fixture(autouse=True)
def _cache_state() -> Iterator[None]:
    """Fresh cache and settings for each test; the previous state is restored."""
    saved = (QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size)
    QueryCache.clear()
    QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size = True, 60, 1000
    try:
        yield
    finally:
        QueryCache.clear()
        QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size = saved


def _fill() -> None:
    QueryCache.set("mine", ["stale"], "u463_items", connection=("default", "http://h:8000", "ns", "db"))
    QueryCache.set("mine-no-conn", ["stale"], "u463_items")
    QueryCache.set("other", ["kept"], "u463_others")


def _assert_untouched() -> None:
    assert QueryCache.get("mine") == ["stale"]
    assert QueryCache.get("mine-no-conn") == ["stale"]
    assert QueryCache.get("other") == ["kept"]


_WRITES: dict[str, Callable[[], Awaitable[Any]]] = {
    "atomic_append": lambda: U463Item.atomic_append("r1", "tags", "x"),
    "atomic_remove": lambda: U463Item.atomic_remove("r1", "tags", "x"),
    "atomic_set_add": lambda: U463Item.atomic_set_add("r1", "tags", "x"),
    "bulk_update": lambda: U463Item.objects().filter(age__gte=1).bulk_update({"name": "n"}),
    "bulk_update_atomic": lambda: U463Item.objects().filter(age__gte=1).bulk_update({"name": "n"}, atomic=True),
    "bulk_delete": lambda: U463Item.objects().filter(age__gte=1).bulk_delete(),
    "bulk_delete_atomic": lambda: U463Item.objects().filter(age__gte=1).bulk_delete(atomic=True),
    "delete_table": lambda: U463Item.objects().delete_table(),
}


class TestEveryWriteInvalidates:
    """Writes that fire no model signal still drop their table's entries, and only those."""

    @pytest.mark.parametrize("write", list(_WRITES))
    async def test_write_drops_only_its_table(self, write: str) -> None:
        _fill()
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client())),
            patch.object(SurrealDBConnectionManager, "transaction", AsyncMock(return_value=_Transaction())),
        ):
            await _WRITES[write]()

        assert QueryCache.get("mine") is None
        assert QueryCache.get("mine-no-conn") is None
        assert QueryCache.get("other") == ["kept"]


class TestRejectedWriteKeepsTheCache:
    """Invalidation runs once the write is accepted, never before."""

    @pytest.mark.parametrize("helper", ["atomic_append", "atomic_remove", "atomic_set_add"])
    async def test_rejected_atomic_helper(self, helper: str) -> None:
        _fill()
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client(_err()))),
            pytest.raises(QueryError),
        ):
            await getattr(U463Item, helper)("r1", "tags", "x")
        _assert_untouched()

    @pytest.mark.parametrize("write", ["bulk_update", "bulk_delete"])
    async def test_rejected_plain_bulk_statement(self, write: str) -> None:
        """The plain bulk paths return a count; a rejected statement wrote nothing."""
        _fill()
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client(_err()))):
            await _WRITES[write]()
        _assert_untouched()

    @pytest.mark.parametrize("write", ["bulk_update_atomic", "bulk_delete_atomic"])
    async def test_rejected_commit(self, write: str) -> None:
        _fill()
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client())),
            patch.object(SurrealDBConnectionManager, "transaction", AsyncMock(return_value=_Transaction(fail_commit=True))),
            pytest.raises(QueryError),
        ):
            await _WRITES[write]()
        _assert_untouched()

    async def test_failed_delete_table(self) -> None:
        _fill()
        client = _client()
        client.delete = AsyncMock(side_effect=QueryError("rejected"))
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)),
            pytest.raises(QueryError),
        ):
            await U463Item.objects().delete_table()
        _assert_untouched()


@pytest.fixture
def _never_configured() -> Iterator[None]:
    """No handler connected and ``_signals_connected`` reset; restored afterwards."""
    signals = (post_save, post_update, post_delete)
    saved = [{k: list(v) for k, v in sig._handlers.items()} for sig in signals]
    saved_flag = QueryCache._signals_connected
    for sig in signals:
        sig._handlers.clear()
    QueryCache._signals_connected = False
    try:
        yield
    finally:
        for sig, handlers in zip(signals, saved, strict=True):
            sig._handlers.clear()
            sig._handlers.update(handlers)
        QueryCache._signals_connected = saved_flag


@pytest.mark.usefixtures("_never_configured")
class TestSignalsConnectWithoutConfigure:
    """Invalidation does not depend on a ``configure()`` call."""

    @staticmethod
    def _receivers() -> list[int]:
        return [len(sig._handlers.get(None, [])) for sig in (post_save, post_update, post_delete)]

    def test_set_connects_the_handlers_once(self) -> None:
        QueryCache.set("k1", [1], "u463_items")
        QueryCache.set("k2", [2], "u463_items")
        assert QueryCache._signals_connected is True
        assert self._receivers() == [1, 1, 1]

    def test_queryset_cache_connects_the_handlers_once(self) -> None:
        U463Item.objects().cache(ttl=30)
        U463Item.objects().cache(ttl=30)
        assert QueryCache._signals_connected is True
        assert self._receivers() == [1, 1, 1]
        QueryCache.configure()
        assert self._receivers() == [1, 1, 1]

    async def test_save_drops_the_entry_without_configure(self) -> None:
        QueryCache.set("k", ["stale"], "u463_items")
        client = _client()
        client.upsert = AsyncMock(return_value={"id": "u463_items:r1", "name": "n"})
        client.merge = AsyncMock(return_value={"id": "u463_items:r1", "name": "n"})
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
            await U463Item(id="r1", name="n").save()
        assert QueryCache.get("k") is None
