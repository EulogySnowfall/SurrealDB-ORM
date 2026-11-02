"""Tests for QueryCache — v0.11.0."""

import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.surreal_orm.cache import QueryCache
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.signals import Signal, post_delete, post_save, post_update
from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

# ── Test model ───────────────────────────────────────────────────────────────


class CacheUser(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="cache_users")
    id: str | None = None
    name: str = ""
    age: int = 0


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    """Reset QueryCache state between tests."""
    QueryCache.clear()
    QueryCache._default_ttl = 60
    QueryCache._max_size = 1000
    QueryCache._enabled = True


# ==================== Unit Tests ====================


class TestQueryCacheConfigure:
    """Test configure() and global settings."""

    def test_default_settings(self) -> None:
        assert QueryCache._default_ttl == 60
        assert QueryCache._max_size == 1000
        assert QueryCache._enabled is True

    def test_configure_updates_settings(self) -> None:
        QueryCache.configure(default_ttl=120, max_size=500, enabled=False)
        assert QueryCache._default_ttl == 120
        assert QueryCache._max_size == 500
        assert QueryCache._enabled is False

    def test_configure_connects_signals(self) -> None:
        QueryCache._signals_connected = False
        QueryCache.configure()
        assert QueryCache._signals_connected is True


class TestQueryCacheMakeKey:
    """Test make_key() deterministic hashing."""

    def test_same_inputs_same_key(self) -> None:
        k1 = QueryCache.make_key("SELECT * FROM users", {"_f0": 1}, "users")
        k2 = QueryCache.make_key("SELECT * FROM users", {"_f0": 1}, "users")
        assert k1 == k2

    def test_different_query_different_key(self) -> None:
        k1 = QueryCache.make_key("SELECT * FROM users", {}, "users")
        k2 = QueryCache.make_key("SELECT * FROM orders", {}, "orders")
        assert k1 != k2

    def test_different_variables_different_key(self) -> None:
        k1 = QueryCache.make_key("SELECT * FROM users", {"_f0": 1}, "users")
        k2 = QueryCache.make_key("SELECT * FROM users", {"_f0": 2}, "users")
        assert k1 != k2

    def test_key_is_hex_string(self) -> None:
        key = QueryCache.make_key("q", {}, "t")
        assert len(key) == 64  # SHA-256 hex
        assert all(c in "0123456789abcdef" for c in key)


class TestQueryCacheSetGet:
    """Test set() and get() operations."""

    def test_set_and_get(self) -> None:
        QueryCache.set("k1", [{"name": "Alice"}], "users")
        assert QueryCache.get("k1") == [{"name": "Alice"}]

    def test_get_missing_key(self) -> None:
        assert QueryCache.get("nonexistent") is None

    def test_set_disabled_does_nothing(self) -> None:
        QueryCache._enabled = False
        QueryCache.set("k1", "data", "users")
        assert QueryCache.get("k1") is None

    def test_expiration(self) -> None:
        QueryCache.set("k1", "data", "users", ttl=0)
        # TTL=0 means it expires immediately
        time.sleep(0.01)
        assert QueryCache.get("k1") is None

    def test_custom_ttl(self) -> None:
        QueryCache.set("k1", "data", "users", ttl=3600)
        assert QueryCache.get("k1") == "data"


class TestQueryCacheInvalidate:
    """Test invalidate() for table-based clearing."""

    def test_invalidate_removes_table_entries(self) -> None:
        QueryCache.set("k1", "data1", "cache_users")
        QueryCache.set("k2", "data2", "cache_users")
        QueryCache.set("k3", "data3", "orders")
        count = QueryCache.invalidate(CacheUser)
        assert count == 2
        assert QueryCache.get("k1") is None
        assert QueryCache.get("k2") is None
        assert QueryCache.get("k3") == "data3"

    def test_invalidate_unknown_table(self) -> None:
        count = QueryCache.invalidate(CacheUser)
        assert count == 0


class TestSignalInvalidationEveryBucket:
    """Issue #338 — a write drops the table's entries cached from every connection.

    The bucket a read was cached in is a raw ``(url, namespace, database)``
    triple, so one database reached through two URL spellings is two buckets,
    and a ``tx=`` write may not run on the connection the model routes to.
    Invalidation therefore never trusts the writer's location.
    """

    _SPELLINGS = (
        ("i338_plain", "http://localhost:8000", "ns", "db"),
        ("i338_slash", "http://localhost:8000/", "ns", "db"),
        ("i338_other", "ws://127.0.0.1:8000", "ns", "db"),
    )

    def _fill(self) -> list[str]:
        keys = []
        for identity in self._SPELLINGS:
            key = QueryCache.make_key("q", {}, "cache_users", connection=identity)
            QueryCache.set(key, [identity[1]], "cache_users", connection=identity)
            keys.append(key)
        QueryCache.set("no-conn", ["unknown"], "cache_users")
        keys.append("no-conn")
        QueryCache.set("other-table", "x", "orders", connection=self._SPELLINGS[0])
        return keys

    @pytest.mark.parametrize("signal", [post_save, post_update, post_delete], ids=lambda s: s.name)
    async def test_signal_drops_the_table_in_every_bucket(self, signal: Signal) -> None:
        QueryCache._connect_signals()
        keys = self._fill()
        await signal.send(sender=CacheUser, instance=CacheUser(id="r1"))
        assert [QueryCache.get(k) for k in keys] == [None] * len(keys)
        assert QueryCache.get("other-table") == "x"
        assert set(QueryCache._table_keys) == {(("http://localhost:8000", "ns", "db"), "orders")}


class TestQueryCacheClear:
    """Test clear() for full cache reset."""

    def test_clear_removes_everything(self) -> None:
        QueryCache.set("k1", "d1", "users")
        QueryCache.set("k2", "d2", "orders")
        QueryCache.clear()
        assert QueryCache.get("k1") is None
        assert QueryCache.get("k2") is None
        assert QueryCache.stats()["entries"] == 0


class TestQueryCacheMaxSize:
    """Test FIFO eviction when max_size is reached."""

    def test_eviction_at_capacity(self) -> None:
        QueryCache._max_size = 3
        QueryCache.set("k1", "d1", "t", ttl=100)
        QueryCache.set("k2", "d2", "t", ttl=200)
        QueryCache.set("k3", "d3", "t", ttl=300)
        # Adding a 4th should evict the oldest inserted entry (k1)
        QueryCache.set("k4", "d4", "t", ttl=400)
        assert len(QueryCache._cache) == 3
        assert QueryCache.get("k1") is None  # evicted
        assert QueryCache.get("k4") == "d4"


class TestQueryCacheStats:
    """Test stats() reporting."""

    def test_stats_structure(self) -> None:
        s = QueryCache.stats()
        assert "entries" in s
        assert "tables" in s
        assert "max_size" in s
        assert "default_ttl" in s
        assert "enabled" in s

    def test_stats_reflects_entries(self) -> None:
        QueryCache.set("k1", "d1", "users")
        QueryCache.set("k2", "d2", "orders")
        s = QueryCache.stats()
        assert s["entries"] == 2
        assert s["tables"] == 2


class TestQuerySetCacheMethod:
    """Test QuerySet.cache() integration."""

    def test_cache_sets_ttl(self) -> None:
        qs = CacheUser.objects().cache(ttl=30)
        assert qs._cache_ttl == 30

    def test_cache_default_ttl(self) -> None:
        QueryCache._default_ttl = 120
        qs = CacheUser.objects().cache()
        assert qs._cache_ttl == 120

    def test_cache_disabled_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        QueryCache._enabled = False
        import logging

        with caplog.at_level(logging.WARNING):
            CacheUser.objects().cache()
        assert "disabled" in caplog.text.lower()


class TestQueryCacheExport:
    """Test that QueryCache is properly exported."""

    def test_import_from_surreal_orm(self) -> None:
        from src.surreal_orm import QueryCache as QC

        assert QC is QueryCache

    def test_in_all(self) -> None:
        import src.surreal_orm as orm

        assert "QueryCache" in orm.__all__


# ── Issue #267: every ORM write invalidates, with or without configure() ────


def _u267_ok(records: list[dict[str, Any]] | None = None) -> QueryResponse:
    """One statement SurrealDB accepted."""
    return QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=records or [])])


def _u267_client() -> AsyncMock:
    client = AsyncMock()
    client.query = AsyncMock(return_value=_u267_ok([{"id": "cache_users:r1", "name": "n", "count": 1}]))
    client.delete = AsyncMock(return_value=None)
    return client


class _U267Transaction:
    """Stands in for ``HTTPTransaction``: an async context manager with ``query``."""

    def __init__(self) -> None:
        self.query = AsyncMock(return_value=_u267_ok([{"id": "cache_users:r1", "name": "n"}]))

    async def __aenter__(self) -> "_U267Transaction":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _u267_fill() -> None:
    QueryCache.set("mine", ["stale"], "cache_users", connection=("default", "http://h:8000", "ns", "db"))
    QueryCache.set("mine-no-conn", ["stale"], "cache_users")
    QueryCache.set("other", ["kept"], "orders")


_U267_WRITES: dict[str, Callable[[], Awaitable[Any]]] = {
    "atomic_append": lambda: CacheUser.atomic_append("r1", "tags", "x"),
    "atomic_remove": lambda: CacheUser.atomic_remove("r1", "tags", "x"),
    "atomic_set_add": lambda: CacheUser.atomic_set_add("r1", "tags", "x"),
    "bulk_update": lambda: CacheUser.objects().filter(age__gte=1).bulk_update({"name": "n"}),
    "bulk_update_atomic": lambda: CacheUser.objects().filter(age__gte=1).bulk_update({"name": "n"}, atomic=True),
    "bulk_delete": lambda: CacheUser.objects().filter(age__gte=1).bulk_delete(),
    "bulk_delete_atomic": lambda: CacheUser.objects().filter(age__gte=1).bulk_delete(atomic=True),
    "upsert": lambda: CacheUser.objects().upsert({"name": "n"}, id="r1"),
    "upsert_on_conflict": lambda: CacheUser.objects().upsert({"name": "n"}, id="r1", on_conflict={"age": 2}),
    "bulk_upsert": lambda: CacheUser.objects().bulk_upsert([CacheUser(id="r1", name="n")]),
    "bulk_upsert_atomic": lambda: CacheUser.objects().bulk_upsert([CacheUser(id="r1", name="n")], atomic=True),
    "delete_table": lambda: CacheUser.objects().delete_table(),
}


class TestEveryWriteInvalidates:
    """Issue #267 — writes that fire no model signal still drop the table's entries."""

    @pytest.mark.parametrize("write", list(_U267_WRITES))
    async def test_write_drops_only_its_table(self, write: str) -> None:
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        _u267_fill()
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_u267_client())),
            patch.object(SurrealDBConnectionManager, "transaction", AsyncMock(return_value=_U267Transaction())),
        ):
            await _U267_WRITES[write]()

        assert QueryCache.get("mine") is None
        assert QueryCache.get("mine-no-conn") is None
        assert QueryCache.get("other") == ["kept"]

    @pytest.mark.parametrize("helper", ["atomic_append", "atomic_remove", "atomic_set_add"])
    async def test_rejected_atomic_write_raises_before_invalidating(self, helper: str) -> None:
        """Only an accepted write has to invalidate; a rejected one raises first."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager
        from surreal_sdk.exceptions import QueryError

        client = _u267_client()
        client.query = AsyncMock(return_value=QueryResponse(results=[QueryResult(status=ResponseStatus.ERR, result="nope")]))
        _u267_fill()
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)),
            pytest.raises(QueryError),
        ):
            await getattr(CacheUser, helper)("r1", "tags", "x")
        assert QueryCache.get("other") == ["kept"]


@pytest.fixture
def _u267_fresh_signals() -> Iterator[None]:
    """No handler connected and ``_signals_connected`` reset; restored afterwards."""
    signals = (post_save, post_update, post_delete)
    saved = [dict((k, list(v)) for k, v in sig._handlers.items()) for sig in signals]
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


@pytest.mark.usefixtures("_u267_fresh_signals")
class TestSignalsConnectWithoutConfigure:
    """Issue #267 — invalidation does not depend on a ``configure()`` call."""

    @staticmethod
    def _receivers() -> list[int]:
        return [len(sig._handlers.get(None, [])) for sig in (post_save, post_update, post_delete)]

    def test_set_connects_the_handlers_once(self) -> None:
        QueryCache.set("k1", [1], "cache_users")
        QueryCache.set("k2", [2], "cache_users")
        assert QueryCache._signals_connected is True
        assert self._receivers() == [1, 1, 1]

    def test_queryset_cache_connects_the_handlers_once(self) -> None:
        CacheUser.objects().cache(ttl=30)
        CacheUser.objects().cache(ttl=30)
        assert QueryCache._signals_connected is True
        assert self._receivers() == [1, 1, 1]
        QueryCache.configure()
        assert self._receivers() == [1, 1, 1]

    async def test_save_drops_the_entry_without_configure(self) -> None:
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        QueryCache.set("k", ["stale"], "cache_users")
        client = _u267_client()
        client.upsert = AsyncMock(return_value={"id": "cache_users:r1", "name": "n"})
        client.merge = AsyncMock(return_value={"id": "cache_users:r1", "name": "n"})
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
            await CacheUser(id="r1", name="n").save()
        assert QueryCache.get("k") is None


# ==================== Integration Tests ====================


@pytest.fixture(scope="module", autouse=True)
async def _setup_connection() -> AsyncGenerator[None, None]:
    """Set up ORM connection for integration tests."""
    from src.surreal_orm import SurrealDBConnectionManager
    from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        "test_cache",
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


@pytest.mark.integration
class TestQueryCacheIntegration:
    """Integration tests requiring a live SurrealDB instance."""

    @pytest.fixture(autouse=True)
    async def setup_data(self) -> None:
        """Create test data."""
        from src.surreal_orm import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()
        await client.query("DELETE FROM cache_users;")
        await client.query("CREATE cache_users:alice SET name = 'Alice', age = 30;")
        await client.query("CREATE cache_users:bob SET name = 'Bob', age = 25;")
        QueryCache.clear()
        QueryCache.configure(enabled=True, default_ttl=60)

    async def test_cache_hit_avoids_db(self) -> None:
        """Second .cache().exec() should return cached data."""
        # First call — cache miss, hits DB
        result1 = await CacheUser.objects().cache(ttl=60).exec()
        assert len(result1) >= 2

        stats_before = QueryCache.stats()
        assert stats_before["entries"] >= 1

        # Second call — cache hit
        result2 = await CacheUser.objects().cache(ttl=60).exec()
        assert len(result2) == len(result1)

    async def test_invalidation_on_save(self) -> None:
        """Saving a model should invalidate cached entries for that table."""
        # Populate cache
        await CacheUser.objects().cache(ttl=60).exec()
        assert QueryCache.stats()["entries"] >= 1

        # Save a new record — should trigger post_save → invalidate
        user = CacheUser(id="charlie", name="Charlie", age=40)
        await user.save()

        # Cache should be empty for this table
        assert QueryCache.stats()["entries"] == 0

    async def test_invalidation_on_delete(self) -> None:
        """Deleting a model should invalidate cached entries."""
        await CacheUser.objects().cache(ttl=60).exec()
        assert QueryCache.stats()["entries"] >= 1

        # Delete a record
        alice = await CacheUser.objects().get("alice")
        await alice.delete()

        assert QueryCache.stats()["entries"] == 0


# ── Issue #338: invalidation reaches every connection ───────────────────────

_I338_DB = "test_cache_i338"
_I338_OTHER_DB = "test_cache_i338_other"


class I338CacheItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i338_cache_items")
    id: str | None = None
    label: str = ""


class I338RoutedItem(BaseSurrealModel):
    """Same table, but routed to a connection on another database."""

    model_config = SurrealConfigDict(table_name="i338_cache_items", connection="i338_other")
    id: str | None = None
    label: str = ""


async def _i338_labels(connection: str) -> list[str]:
    from src.surreal_orm import SurrealDBConnectionManager

    async with SurrealDBConnectionManager.using(connection):
        rows = await I338CacheItem.objects().cache(ttl=60).exec()
    return sorted(row.label for row in rows)


@pytest.mark.integration
class TestQueryCacheInvalidationEveryConnection:
    """Issue #338 — a write never leaves a stale ``.cache()`` result behind."""

    @pytest.fixture(autouse=True)
    async def i338_connections(self) -> AsyncGenerator[None, None]:
        from src.surreal_orm import SurrealDBConnectionManager
        from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

        # One database, two spellings of its URL: two cache buckets.
        connections = {
            "i338_plain": (SURREALDB_URL.rstrip("/"), _I338_DB),
            "i338_slash": (SURREALDB_URL.rstrip("/") + "/", _I338_DB),
            "i338_other": (SURREALDB_URL, _I338_OTHER_DB),
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
        for name in ("i338_plain", "i338_other"):
            client = await SurrealDBConnectionManager.get_client(name)
            await client.query("DELETE FROM i338_cache_items;")
        client = await SurrealDBConnectionManager.get_client("i338_plain")
        await client.query("CREATE i338_cache_items:r1 SET label = 'one';")
        QueryCache.configure(enabled=True, default_ttl=60)
        QueryCache.clear()
        yield
        QueryCache.clear()
        for name in connections:
            await SurrealDBConnectionManager.remove_connection(name)

    async def test_save_through_one_spelling_invalidates_the_other(self) -> None:
        from src.surreal_orm import SurrealDBConnectionManager

        assert await _i338_labels("i338_slash") == ["one"]
        async with SurrealDBConnectionManager.using("i338_plain"):
            await I338CacheItem(id="r2", label="two").save()
        assert await _i338_labels("i338_slash") == ["one", "two"]

    async def test_save_in_a_transaction_invalidates_whatever_the_routing(self) -> None:
        from src.surreal_orm import SurrealDBConnectionManager

        assert await _i338_labels("i338_plain") == ["one"]
        # The model routes to i338_other; the transaction runs on i338_plain.
        client = await SurrealDBConnectionManager.get_client("i338_plain")
        async with client.transaction() as tx:
            await I338RoutedItem(id="r3", label="three").save(tx=tx)
        assert await _i338_labels("i338_plain") == ["one", "three"]
