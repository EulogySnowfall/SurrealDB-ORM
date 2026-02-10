"""Tests for QueryCache — v0.11.0."""

import time
from typing import AsyncGenerator

import pytest

from src.surreal_orm.cache import QueryCache
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict


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


# ==================== Integration Tests ====================


@pytest.fixture(scope="module", autouse=True)
async def _setup_connection() -> AsyncGenerator[None, None]:
    """Set up ORM connection for integration tests."""
    from src.surreal_orm import SurrealDBConnectionManager

    SurrealDBConnectionManager.set_connection(
        "http://localhost:8001",
        "root",
        "root",
        "test",
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
