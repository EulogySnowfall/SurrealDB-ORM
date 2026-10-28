"""
Query cache for the ORM with TTL-based expiration and auto-invalidation.

``QueryCache`` is a class-level singleton that caches query results keyed by
a SHA-256 hash of the query string, variables, table name, and the identity of
the connection the query runs on (name, URL, namespace and database of its
``ConnectionConfig`` — never the credentials).  Two named connections pointing
at different databases therefore never share entries.

Cache entries are automatically invalidated when models are saved, updated, or
deleted via the ``post_save``, ``post_update``, and ``post_delete`` signals.
The signal handlers only drop the entries of the database the write went to
(every connection name pointing at that URL/namespace/database), so a write
through one connection leaves another database's cached results in place.

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

ConnectionIdentity = tuple[str, str | None, str | None, str | None]
"""``(name, url, namespace, database)`` of the connection a query runs on."""

_Location = tuple[str | None, str | None, str | None]
"""``(url, namespace, database)``: where the data lives, whatever the name."""

_NO_LOCATION: _Location = (None, None, None)


@dataclass(slots=True)
class _CacheEntry:
    """A single cached query result with expiration timestamp."""

    data: Any
    table: str
    expires_at: float
    location: _Location = _NO_LOCATION


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
    _table_keys: dict[tuple[_Location, str], set[str]] = {}  # (location, table) → cache keys
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
    def make_key(
        cls,
        query: str,
        variables: dict[str, Any],
        table: str,
        *,
        connection: ConnectionIdentity | None = None,
    ) -> str:
        """
        Build a deterministic cache key from the query, variables, and table.

        Args:
            query: The compiled SurrealQL query string.
            variables: The parameterized variable bindings.
            table: The table name.
            connection: Identity of the connection the query runs on, as
                returned by ``_connection_identity()``.  ``QuerySet.cache()``
                always passes it, so the same query on two databases gets two
                keys.  Omitted, the key depends on the query alone.

        Returns:
            A hex SHA-256 digest.
        """
        body: dict[str, Any] = {"q": query, "v": variables, "t": table}
        if connection is not None:
            body["c"] = list(connection)
        payload = json.dumps(body, sort_keys=True, default=str)
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
        *,
        connection: ConnectionIdentity | None = None,
    ) -> None:
        """
        Store a query result in the cache.

        Args:
            key: The cache key (from ``make_key``).
            data: The query result to cache.
            table: The table name (used for targeted invalidation).
            ttl: Time-to-live in seconds.  Defaults to ``_default_ttl``.
            connection: Identity of the connection the result came from, so a
                write through that database invalidates it.  Should be the
                identity passed to ``make_key``.
        """
        if not cls._enabled:
            return

        # Re-setting an existing key must not leave it indexed under a stale bucket.
        cls._remove_key(key)

        # Evict oldest entries if at capacity
        while len(cls._cache) >= cls._max_size:
            cls._evict_oldest()

        location = _location_of(connection)
        expires_at = time.monotonic() + (ttl if ttl is not None else cls._default_ttl)
        cls._cache[key] = _CacheEntry(
            data=copy.deepcopy(data),
            table=table,
            expires_at=expires_at,
            location=location,
        )
        cls._table_keys.setdefault((location, table), set()).add(key)

    @classmethod
    def invalidate(cls, model: Any) -> int:
        """
        Remove all cached entries for a model's table, on every connection.

        Args:
            model: The model class whose table entries should be cleared.

        Returns:
            The number of entries removed.
        """
        table = model.get_table_name()
        removed = 0
        for bucket in [b for b in cls._table_keys if b[1] == table]:
            removed += cls._drop_bucket(bucket)
        return removed

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
            "tables": len({table for _, table in cls._table_keys}),
            "max_size": cls._max_size,
            "default_ttl": cls._default_ttl,
            "enabled": cls._enabled,
        }

    # ── Internal ─────────────────────────────────────────────────────────

    @staticmethod
    def _connection_identity(model: Any) -> ConnectionIdentity:
        """
        Resolve the identity of the connection *model* queries run on now.

        Follows ``model.get_connection_name()`` (``using()`` override, then the
        model's ``connection`` config, then ``"default"``) and reads the URL,
        namespace and database of that name's ``ConnectionConfig``.  The
        credentials are deliberately left out of the identity.  A name with no
        registered config resolves to ``(name, None, None, None)``.
        """
        from .connection_manager import SurrealDBConnectionManager

        name: str = model.get_connection_name()
        config = SurrealDBConnectionManager.get_config(name)
        if config is None:
            return (name, None, None, None)
        return (name, config.url, config.namespace, config.database)

    @classmethod
    def _invalidate_for_connection(cls, model: Any, connection: ConnectionIdentity) -> int:
        """
        Remove *model*'s table entries cached from *connection*'s database.

        Entries stored without a connection (a direct :meth:`set` with no
        ``connection=``) carry no database, so a write to the table may have
        made them stale whichever connection it ran on: they are dropped too.
        """
        table = model.get_table_name()
        removed = cls._drop_bucket((_location_of(connection), table))
        if _location_of(connection) != _NO_LOCATION:
            removed += cls._drop_bucket((_NO_LOCATION, table))
        return removed

    @classmethod
    def _drop_bucket(cls, bucket: tuple[_Location, str]) -> int:
        keys = cls._table_keys.pop(bucket, set())
        for key in keys:
            cls._cache.pop(key, None)
        return len(keys)

    @classmethod
    def _remove_key(cls, key: str) -> None:
        entry = cls._cache.pop(key, None)
        if entry is not None:
            bucket = (entry.location, entry.table)
            table_keys = cls._table_keys.get(bucket)
            if table_keys is not None:
                table_keys.discard(key)
                if not table_keys:
                    cls._table_keys.pop(bucket, None)

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

        from .signals import SignalHandler, post_delete, post_save, post_update

        # The identity is resolved while the signal is sent, i.e. still inside
        # the ``using()`` context of the write, so only the database written to
        # loses its entries.

        async def _invalidate_on_save(sender: type, **kwargs: Any) -> None:
            cls._invalidate_for_connection(sender, cls._connection_identity(sender))

        async def _invalidate_on_delete(sender: type, **kwargs: Any) -> None:
            cls._invalidate_for_connection(sender, cls._connection_identity(sender))

        async def _invalidate_on_update(sender: type, **kwargs: Any) -> None:
            cls._invalidate_for_connection(sender, cls._connection_identity(sender))

        _on_save: SignalHandler = _invalidate_on_save
        _on_delete: SignalHandler = _invalidate_on_delete
        _on_update: SignalHandler = _invalidate_on_update

        post_save.connect()(_on_save)
        post_delete.connect()(_on_delete)
        post_update.connect()(_on_update)

        cls._signals_connected = True


def _location_of(connection: ConnectionIdentity | None) -> _Location:
    """Drop the name: every name pointing at one database shares a bucket."""
    if connection is None:
        return _NO_LOCATION
    _, url, namespace, database = connection
    return (url, namespace, database)


__all__ = ["QueryCache"]
