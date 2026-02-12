"""
Query cache for the ORM with TTL-based expiration and auto-invalidation.

``QueryCache`` is a class-level singleton that caches query results keyed by
a SHA-256 hash of the query string, variables, and table name.  Cache entries
are automatically invalidated when models are saved, updated, or deleted via
the ``post_save``, ``post_update``, and ``post_delete`` signals.

Example::

    from surreal_orm import QueryCache

    # Configure the cache (call once at startup)
    QueryCache.configure(default_ttl=120, max_size=500)

    # Use .cache() on any QuerySet
    users = await User.objects().filter(role="admin").cache(ttl=60).exec()

    # Manual invalidation
    QueryCache.invalidate(User)

    # Disable cache globally
    QueryCache.configure(enabled=False)
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _CacheEntry:
    """A single cached query result with expiration timestamp."""

    data: Any
    table: str
    expires_at: float


class QueryCache:
    """
    Global query cache with TTL, FIFO eviction, and signal-based invalidation.

    This is a class-level singleton — all configuration and state is stored as
    class attributes.  Call ``configure()`` once at startup to set defaults.
    """

    # ── Configuration ────────────────────────────────────────────────────

    _default_ttl: int = 60  # seconds
    _max_size: int = 1000
    _enabled: bool = True

    # ── State ────────────────────────────────────────────────────────────

    _cache: dict[str, _CacheEntry] = {}
    _table_keys: dict[str, set[str]] = {}  # table → set of cache keys
    _signals_connected: bool = False

    # ── Public API ───────────────────────────────────────────────────────

    @classmethod
    def configure(
        cls,
        *,
        default_ttl: int = 60,
        max_size: int = 1000,
        enabled: bool = True,
    ) -> None:
        """
        Configure global cache settings.

        Connecting to signals is done lazily on first call.  Calling
        ``configure()`` again updates settings without clearing the cache.

        Args:
            default_ttl: Default time-to-live in seconds for cache entries.
            max_size: Maximum number of cached entries (FIFO eviction).
            enabled: Whether the cache is active.
        """
        cls._default_ttl = default_ttl
        cls._max_size = max_size
        cls._enabled = enabled
        cls._connect_signals()

    @classmethod
    def make_key(cls, query: str, variables: dict[str, Any], table: str) -> str:
        """
        Build a deterministic cache key from the query, variables, and table.

        Args:
            query: The compiled SurrealQL query string.
            variables: The parameterized variable bindings.
            table: The table name.

        Returns:
            A hex SHA-256 digest.
        """
        payload = json.dumps(
            {"q": query, "v": variables, "t": table},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    @classmethod
    def get(cls, key: str) -> Any | None:
        """
        Retrieve a cached result by key.

        Returns ``None`` if the cache is disabled, the key is missing, or
        the entry has expired (expired entries are removed on access).

        A deep copy of the stored data is returned so that callers cannot
        accidentally mutate the cached value.
        """
        if not cls._enabled:
            return None
        entry = cls._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            cls._remove_key(key)
            return None
        return copy.deepcopy(entry.data)

    @classmethod
    def set(
        cls,
        key: str,
        data: Any,
        table: str,
        ttl: int | None = None,
    ) -> None:
        """
        Store a query result in the cache.

        Args:
            key: The cache key (from ``make_key``).
            data: The query result to cache.
            table: The table name (used for targeted invalidation).
            ttl: Time-to-live in seconds.  Defaults to ``_default_ttl``.
        """
        if not cls._enabled:
            return

        # Evict oldest entries if at capacity
        while len(cls._cache) >= cls._max_size:
            cls._evict_oldest()

        expires_at = time.monotonic() + (ttl if ttl is not None else cls._default_ttl)
        cls._cache[key] = _CacheEntry(data=copy.deepcopy(data), table=table, expires_at=expires_at)

        if table not in cls._table_keys:
            cls._table_keys[table] = set()
        cls._table_keys[table].add(key)

    @classmethod
    def invalidate(cls, model: Any) -> int:
        """
        Remove all cached entries for a model's table.

        Args:
            model: The model class whose table entries should be cleared.

        Returns:
            The number of entries removed.
        """
        table = model.get_table_name()
        keys = cls._table_keys.pop(table, set())
        for key in keys:
            cls._cache.pop(key, None)
        return len(keys)

    @classmethod
    def clear(cls) -> None:
        """Remove all entries from the cache."""
        cls._cache.clear()
        cls._table_keys.clear()

    @classmethod
    def stats(cls) -> dict[str, Any]:
        """
        Return cache statistics.

        Returns:
            Dict with ``entries``, ``tables``, ``max_size``, ``default_ttl``,
            and ``enabled``.
        """
        return {
            "entries": len(cls._cache),
            "tables": len(cls._table_keys),
            "max_size": cls._max_size,
            "default_ttl": cls._default_ttl,
            "enabled": cls._enabled,
        }

    # ── Internal ─────────────────────────────────────────────────────────

    @classmethod
    def _remove_key(cls, key: str) -> None:
        entry = cls._cache.pop(key, None)
        if entry is not None:
            table_keys = cls._table_keys.get(entry.table)
            if table_keys is not None:
                table_keys.discard(key)
                if not table_keys:
                    cls._table_keys.pop(entry.table, None)

    @classmethod
    def _evict_oldest(cls) -> None:
        """Evict the oldest cache entry based on insertion order (FIFO)."""
        if not cls._cache:
            return
        # dict preserves insertion order in Python 3.7+
        oldest_key = next(iter(cls._cache))
        cls._remove_key(oldest_key)

    @classmethod
    def _connect_signals(cls) -> None:
        """Lazily connect to ORM signals for auto-invalidation."""
        if cls._signals_connected:
            return

        from .signals import post_delete, post_save, post_update

        @post_save.connect()
        async def _invalidate_on_save(sender: type, **kwargs: Any) -> None:
            cls.invalidate(sender)

        @post_delete.connect()
        async def _invalidate_on_delete(sender: type, **kwargs: Any) -> None:
            cls.invalidate(sender)

        @post_update.connect()
        async def _invalidate_on_update(sender: type, **kwargs: Any) -> None:
            cls.invalidate(sender)

        cls._signals_connected = True


__all__ = ["QueryCache"]
