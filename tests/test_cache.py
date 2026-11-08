"""Tests for QueryCache — v0.11.0."""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.cache import QueryCache
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.signals import Signal, post_delete, post_save, post_update
from surreal_sdk.transaction import BaseTransaction, HTTPTransaction, WebSocketTransaction
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


# ── Issue #360: a transactional write drops the table's entries again at commit ──


class I360CacheUser(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i360_cache_users")
    id: str | None = None
    name: str = ""


class _I360Database:
    """The rows a read sees: ``before`` until a commit reaches the server, ``after`` once it has."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [{"id": "i360_cache_users:r1", "name": "before"}]

    def commit(self) -> None:
        self.rows = [{"id": "i360_cache_users:r1", "name": "after"}]

    def read_client(self) -> AsyncMock:
        async def _query(*args: Any, **kwargs: Any) -> QueryResponse:
            return _u267_ok([dict(row) for row in self.rows])

        client = AsyncMock()
        client.query = AsyncMock(side_effect=_query)
        return client

    def tx_connection(self) -> MagicMock:
        """What the transaction talks to: the ``COMMIT`` makes the write visible."""

        async def _query(sql: str, *args: Any, **kwargs: Any) -> QueryResponse:
            if "COMMIT TRANSACTION" in sql:
                self.commit()
            return _u267_ok([])

        conn = MagicMock()
        conn.query = AsyncMock(side_effect=_query)
        for method in ("create", "upsert", "merge", "update", "delete"):
            setattr(conn, method, AsyncMock(return_value=MagicMock(records=[{"id": "i360_cache_users:r1"}], is_empty=False)))
        return conn


async def _i360_names() -> list[str]:
    return [user.name for user in await I360CacheUser.objects().cache(ttl=60).exec()]


def _i360_reading_before_commit(base: type[BaseTransaction], seen: list[list[str]]) -> type[BaseTransaction]:
    """A transaction that runs the cached read from another task right before it commits."""

    class _ReadBeforeCommit(base):  # type: ignore[misc,valid-type]
        async def commit(self) -> QueryResponse:
            seen.append(await asyncio.create_task(_i360_names()))
            return await super().commit()  # type: ignore[no-any-return]

    return _ReadBeforeCommit


def _i360_persisted() -> I360CacheUser:
    user = I360CacheUser.from_db({"id": "i360_cache_users:r1", "name": "before"})
    assert isinstance(user, I360CacheUser)
    return user


async def _i360_update(tx: BaseTransaction) -> None:
    user = _i360_persisted()
    user.name = "after"
    await user.update(tx=tx)


_I360_WRITES: dict[str, Callable[[BaseTransaction], Awaitable[Any]]] = {
    "save": lambda tx: I360CacheUser(id="r2", name="after").save(tx=tx),
    "update": _i360_update,
    "merge": lambda tx: _i360_persisted().merge(name="after", tx=tx),
    "delete": lambda tx: _i360_persisted().delete(tx=tx),
}


class TestI360InvalidateAgainOnCommit:
    """Issue #360 — a read cached between a ``tx=`` write and its commit does not outlive the commit."""

    @pytest.fixture(autouse=True)
    def _signals(self) -> None:
        QueryCache._connect_signals()

    @pytest.mark.parametrize("kind", ["http", "websocket"])
    @pytest.mark.parametrize("write", list(_I360_WRITES))
    async def test_read_after_commit_sees_the_commit(self, write: str, kind: str) -> None:
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        db = _I360Database()
        seen: list[list[str]] = []
        base: type[BaseTransaction] = HTTPTransaction if kind == "http" else WebSocketTransaction
        tx_class = _i360_reading_before_commit(base, seen)
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=db.read_client())):
            assert await _i360_names() == ["before"]
            async with tx_class(db.tx_connection()) as tx:
                await _I360_WRITES[write](tx)
            # The other task cached the pre-commit rows inside the window.
            assert seen == [["before"]]
            assert await _i360_names() == ["after"]

    async def test_bulk_create_atomic(self) -> None:
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        db = _I360Database()
        seen: list[list[str]] = []
        tx = _i360_reading_before_commit(HTTPTransaction, seen)(db.tx_connection())
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=db.read_client())),
            patch.object(SurrealDBConnectionManager, "transaction", AsyncMock(return_value=tx)),
        ):
            assert await _i360_names() == ["before"]
            await I360CacheUser.objects().bulk_create([I360CacheUser(id="r2", name="n2")], atomic=True)
            assert seen == [["before"]]
            assert await _i360_names() == ["after"]

    async def test_rollback_leaves_the_entries_and_does_not_fail(self) -> None:
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        db = _I360Database()
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=db.read_client())):
            with pytest.raises(RuntimeError, match="abort"):
                async with HTTPTransaction(db.tx_connection()) as tx:
                    await I360CacheUser(id="r2", name="after").save(tx=tx)
                    assert await _i360_names() == ["before"]  # cached inside the window
                    raise RuntimeError("abort")
            assert tx.is_rolled_back
            assert tx._commit_callbacks == {}
            # Nothing was committed, so the entry cached in the window is still right.
            assert await _i360_names() == ["before"]

    async def test_one_callback_per_table_and_transaction(self) -> None:
        tx = HTTPTransaction(_I360Database().tx_connection())
        await tx._begin()
        await I360CacheUser(id="a", name="a").save(tx=tx)
        await I360CacheUser(id="b", name="b").save(tx=tx)
        await CacheUser(id="c", name="c").save(tx=tx)
        assert len(tx._commit_callbacks) == 2
        await tx.rollback()

    async def test_callback_keeps_neither_transaction_nor_instance(self) -> None:
        import gc
        import weakref

        db = _I360Database()
        tx = HTTPTransaction(db.tx_connection())
        await tx._begin()
        user = I360CacheUser(id="r2", name="after")
        await user.save(tx=tx)
        tx_ref, user_ref = weakref.ref(tx), weakref.ref(user)
        callbacks = list(tx._commit_callbacks.values())
        del tx, user
        gc.collect()
        assert callbacks
        assert tx_ref() is None
        assert user_ref() is None


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


# ── Issue #360: a transactional write drops the table's entries again at commit ──


class I360CacheItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i360_cache_items")
    id: str | None = None
    label: str = ""


async def _i360_labels() -> list[str]:
    return sorted(row.label for row in await I360CacheItem.objects().cache(ttl=60).exec())


@pytest.mark.integration
class TestI360InvalidateAgainOnCommitIntegration:
    """Issue #360 — over HTTP, a read cached before the commit does not outlive it."""

    @pytest.fixture(autouse=True)
    async def i360_data(self) -> AsyncGenerator[None, None]:
        from src.surreal_orm import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()
        await client.query("DELETE FROM i360_cache_items; CREATE i360_cache_items:one SET label = 'one';")
        QueryCache.configure(enabled=True, default_ttl=60)
        QueryCache.clear()
        yield
        QueryCache.clear()

    async def test_save_in_a_transaction(self) -> None:
        from src.surreal_orm import SurrealDBConnectionManager

        assert await _i360_labels() == ["one"]
        async with await SurrealDBConnectionManager.transaction() as tx:
            await I360CacheItem(id="two", label="two").save(tx=tx)
            # Another task reads (and caches) the table before the commit.
            assert await asyncio.create_task(_i360_labels()) == ["one"]
        assert await _i360_labels() == ["one", "two"]

    async def test_bulk_create_atomic(self) -> None:
        commit = HTTPTransaction.commit
        seen: list[list[str]] = []

        async def _read_then_commit(self: HTTPTransaction) -> QueryResponse:
            seen.append(await asyncio.create_task(_i360_labels()))
            return await commit(self)

        assert await _i360_labels() == ["one"]
        with patch.object(HTTPTransaction, "commit", _read_then_commit):
            await I360CacheItem.objects().bulk_create(
                [I360CacheItem(id="two", label="two"), I360CacheItem(id="three", label="three")], atomic=True
            )
        assert seen == [["one"]]
        assert await _i360_labels() == ["one", "three", "two"]
