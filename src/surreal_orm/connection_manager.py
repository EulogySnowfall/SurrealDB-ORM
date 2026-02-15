"""
Connection manager with multi-database support.

Provides a named connection registry so that different models can route
to different SurrealDB namespaces/databases.  The ``"default"`` connection
is used when no explicit name is given, preserving full backward
compatibility with the single-connection API from earlier versions.

Usage::

    # Legacy (still works, equivalent to add_connection("default", ...))
    SurrealDBConnectionManager.set_connection(url=..., ...)

    # Multi-DB
    SurrealDBConnectionManager.add_connection("analytics", url=..., ns=..., db=...)

    class AnalyticsEvent(BaseSurrealModel):
        model_config = SurrealConfigDict(connection="analytics")

    # Context-manager override (async-safe via contextvars)
    async with SurrealDBConnectionManager.using("analytics"):
        events = await AnalyticsEvent.objects().all()
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from surreal_sdk import HTTPConnection, WebSocketConnection
from surreal_sdk.exceptions import SurrealDBError
from surreal_sdk.transaction import HTTPTransaction

from .connection_config import ConnectionConfig

logger = logging.getLogger(__name__)

# Async-safe context variable for ``using()`` overrides.
_active_connection: contextvars.ContextVar[str | None] = contextvars.ContextVar("active_connection", default=None)


class SurrealDbConnectionError(Exception):
    """Connection error for SurrealDB."""

    pass


class SurrealDBConnectionManager:
    """Named connection registry for SurrealDB.

    Stores ``ConnectionConfig`` objects keyed by name.  ``"default"`` is
    the implicit name used by ``set_connection()`` and all legacy callers.
    """

    # --- registry ----------------------------------------------------------
    _configs: dict[str, ConnectionConfig] = {}
    _clients: dict[str, HTTPConnection] = {}
    _ws_clients: dict[str, WebSocketConnection] = {}
    _connection_lock: asyncio.Lock | None = None

    # --- legacy class-level getters (kept in sync for backward compat) -----
    # These mirror the "default" config so that code like
    # ``SurrealDBConnectionManager.get_url()`` keeps working.
    __url: str | None = None
    __user: str | None = None
    __password: str | None = None
    __namespace: str | None = None
    __database: str | None = None
    __protocol: Literal["json", "cbor"] = "cbor"
    __client: HTTPConnection | None = None
    __ws_client: WebSocketConnection | None = None

    # -----------------------------------------------------------------------
    # Async context-manager (unchanged public API)
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        self._resolved_name: str = "default"

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await SurrealDBConnectionManager.close_connection(self._resolved_name)

    async def __aenter__(self) -> HTTPConnection:
        self._resolved_name = SurrealDBConnectionManager.get_active_connection_name() or "default"
        return await SurrealDBConnectionManager.get_client(self._resolved_name)

    # -----------------------------------------------------------------------
    # Multi-DB public API
    # -----------------------------------------------------------------------

    @classmethod
    def add_connection(
        cls,
        name: str,
        *,
        url: str,
        user: str,
        password: str,
        namespace: str,
        database: str,
        protocol: Literal["json", "cbor"] = "cbor",
    ) -> None:
        """Register a named connection configuration.

        Args:
            name: Connection name (e.g. ``"default"``, ``"analytics"``).
            url: SurrealDB URL.
            user: Username for authentication.
            password: Password for authentication.
            namespace: SurrealDB namespace.
            database: SurrealDB database.
            protocol: ``"json"`` or ``"cbor"`` (default).
        """
        config = ConnectionConfig(
            url=url,
            user=user,
            password=password,
            namespace=namespace,
            database=database,
            protocol=protocol,
        )

        # Invalidate cached clients when config changes so get_client()
        # creates a fresh connection with the new settings.
        old_config = cls._configs.get(name)
        if old_config is not None and old_config != config:
            cls._clients.pop(name, None)
            cls._ws_clients.pop(name, None)
            if name == "default":
                cls.__client = None
                cls.__ws_client = None

        cls._configs[name] = config

        # Keep legacy class vars in sync when touching "default"
        if name == "default":
            cls._sync_legacy_vars(config)

    @classmethod
    async def remove_connection(cls, name: str) -> None:
        """Remove a named connection and properly close its clients."""
        await cls._close_single(name)
        cls._configs.pop(name, None)
        cls._clients.pop(name, None)
        cls._ws_clients.pop(name, None)

        if name == "default":
            cls._clear_legacy_vars()

    @classmethod
    def get_config(cls, name: str = "default") -> ConnectionConfig | None:
        """Return the ``ConnectionConfig`` for *name*, or ``None``."""
        return cls._configs.get(name)

    @classmethod
    def list_connections(cls) -> list[str]:
        """Return the names of all registered connections."""
        return list(cls._configs.keys())

    @classmethod
    def get_active_connection_name(cls) -> str | None:
        """Return the currently active connection name override, or ``None``.

        Returns:
            The connection name set by ``using()``, or ``None`` if no
            override is active.
        """
        return _active_connection.get()

    @classmethod
    @asynccontextmanager
    async def using(cls, name: str) -> AsyncIterator[None]:
        """Temporarily override the active connection name.

        Async-safe via :mod:`contextvars`::

            async with SurrealDBConnectionManager.using("analytics"):
                events = await AnalyticsEvent.objects().all()
        """
        if name not in cls._configs:
            raise ValueError(f"Unknown connection: {name!r}. Register it with add_connection() first.")
        token = _active_connection.set(name)
        try:
            yield
        finally:
            _active_connection.reset(token)

    # -----------------------------------------------------------------------
    # Legacy single-connection API (delegates to "default")
    # -----------------------------------------------------------------------

    @classmethod
    def set_connection(
        cls,
        url: str,
        user: str,
        password: str,
        namespace: str,
        database: str,
        *,
        username: str | None = None,
        protocol: Literal["json", "cbor"] = "cbor",
    ) -> None:
        """
        Set the connection kwargs for the SurrealDB instance.

        This is sugar for ``add_connection("default", ...)``.

        :param url: The URL of the SurrealDB instance.
        :param user: The username for authentication.
        :param password: The password for authentication.
        :param namespace: The namespace to use.
        :param database: The database to use.
        :param username: Keyword-only alias for 'user' (overrides 'user' if provided).
        :param protocol: Serialization protocol ("json" or "cbor"). Defaults to "cbor"
                         which properly handles string values that might be misinterpreted
                         as record links (e.g., data URLs like "data:image/png;base64,...").
        """
        actual_user = username if username is not None else user
        cls.add_connection(
            "default",
            url=url,
            user=actual_user,
            password=password,
            namespace=namespace,
            database=database,
            protocol=protocol,
        )

    @classmethod
    async def unset_connection(cls) -> None:
        """
        Unset the connection kwargs and close any active connections (HTTP + WebSocket).

        This is an async method that properly closes connections before
        clearing the settings. Use unset_connection_sync() if you need a
        synchronous version (e.g., in atexit handlers or non-async contexts).
        """
        await cls.close_connection("default")
        cls._configs.pop("default", None)
        cls._clear_legacy_vars()

    @classmethod
    def unset_connection_sync(cls) -> None:
        """
        Synchronously unset the connection kwargs without closing the connection.

        This method clears all connection settings but does NOT close the active
        connection (since close() is async). Use this in contexts where you cannot
        use async/await, such as:
        - atexit handlers
        - __del__ methods
        - synchronous cleanup code

        For proper cleanup that closes the connection, use the async unset_connection().

        Note: The underlying connection object will be garbage collected, but the
        WebSocket/HTTP session may not be cleanly closed. If possible, prefer
        calling unset_connection() in an async context.
        """
        cls._configs.pop("default", None)
        cls._clients.pop("default", None)
        cls._ws_clients.pop("default", None)
        cls._clear_legacy_vars()

    @classmethod
    def is_connection_set(cls) -> bool:
        """
        Check if the connection kwargs are set.

        :return: True if the connection kwargs are set, False otherwise.
        """
        return "default" in cls._configs

    # -----------------------------------------------------------------------
    # Client accessors (HTTP / WebSocket)
    # -----------------------------------------------------------------------

    @classmethod
    async def get_client(cls, name: str | None = None) -> HTTPConnection:
        """
        Connect to the SurrealDB instance using the custom SDK.

        Args:
            name: Connection name.  ``None`` means use the active connection
                  (context var → ``"default"``).

        :return: The HTTPConnection instance.
        """
        name = name or cls.get_active_connection_name() or "default"

        # Fast path: reuse existing connected client
        existing = cls._clients.get(name)
        if existing is not None and existing.is_connected:
            return existing

        if cls._connection_lock is None:
            cls._connection_lock = asyncio.Lock()
        async with cls._connection_lock:
            # Double-check after acquiring the lock (another coroutine may
            # have created the connection while we were waiting).
            existing = cls._clients.get(name)
            if existing is not None and existing.is_connected:
                return existing

            config = cls._configs.get(name)
            if config is None:
                raise ValueError(f"Connection {name!r} not configured. Call set_connection() or add_connection() first.")

            _client: HTTPConnection | None = None
            try:
                _client = HTTPConnection(
                    config.url,
                    config.namespace,
                    config.database,
                    protocol=config.protocol,
                )
                await _client.connect()
                await _client.signin(config.user, config.password)

                cls._clients[name] = _client

                # Keep legacy alias in sync
                if name == "default":
                    cls.__client = _client

                return _client
            except SurrealDBError as e:
                logger.warning("Can't get connection '%s': %s", name, e)
                if _client is not None:
                    try:
                        await _client.close()
                    except Exception:
                        pass  # Best-effort cleanup; original error is re-raised below
                raise SurrealDbConnectionError(f"Can't connect to the database: {e}")
            except Exception as e:
                logger.warning("Can't get connection '%s': %s", name, e)
                if _client is not None:
                    try:
                        await _client.close()
                    except Exception:
                        pass  # Best-effort cleanup; original error is re-raised below
                raise SurrealDbConnectionError("Can't connect to the database.")

    @classmethod
    async def get_ws_client(cls, name: str | None = None) -> WebSocketConnection:
        """
        Get or create a WebSocket connection to SurrealDB.

        The WebSocket connection is created lazily on first call and reused
        for subsequent calls. It uses the same URL and credentials as the
        HTTP connection, with automatic ``http`` → ``ws`` URL conversion
        (handled by ``WebSocketConnection.__init__``).

        Required for Live Queries (``QuerySet.live()``).

        Args:
            name: Connection name.  ``None`` means use the active connection.

        :return: The WebSocketConnection instance.
        :raises ValueError: If connection settings have not been configured.
        :raises SurrealDbConnectionError: If the WebSocket connection fails.
        """
        name = name or cls.get_active_connection_name() or "default"

        existing = cls._ws_clients.get(name)
        if existing is not None and existing.is_connected:
            return existing

        config = cls._configs.get(name)
        if config is None:
            raise ValueError(f"Connection {name!r} not configured. Call set_connection() or add_connection() first.")

        # Close stale disconnected client before creating a new one
        stale = cls._ws_clients.pop(name, None)
        if stale is not None:
            try:
                await stale.close()
            except Exception:
                logger.debug("Failed to close stale WebSocket client for '%s'.", name, exc_info=True)

        _ws_client: WebSocketConnection | None = None
        try:
            _ws_client = WebSocketConnection(
                config.url,
                config.namespace,
                config.database,
                protocol=config.protocol,
            )
            await _ws_client.connect()
            await _ws_client.signin(config.user, config.password)

            cls._ws_clients[name] = _ws_client

            if name == "default":
                cls.__ws_client = _ws_client

            return _ws_client
        except SurrealDBError as e:
            logger.warning("Can't get WebSocket connection '%s': %s", name, e)
            if _ws_client is not None:  # pragma: no cover
                await _ws_client.close()
            raise SurrealDbConnectionError(f"Can't connect to the database via WebSocket: {e}")
        except Exception as e:
            logger.warning("Can't get WebSocket connection '%s': %s", name, e)
            if _ws_client is not None:  # pragma: no cover
                await _ws_client.close()
            raise SurrealDbConnectionError("Can't connect to the database via WebSocket.")

    @classmethod
    async def close_connection(cls, name: str | None = None) -> None:
        """
        Close connections to SurrealDB.

        Args:
            name: Connection name to close.  ``None`` closes **all** connections.
        """
        if name is None:
            # Close all connections
            names = list(cls._clients.keys()) + list(cls._ws_clients.keys())
            for n in set(names):
                await cls._close_single(n)
            cls._clients.clear()
            cls._ws_clients.clear()
            cls.__client = None
            cls.__ws_client = None
        else:
            await cls._close_single(name)
            cls._clients.pop(name, None)
            cls._ws_clients.pop(name, None)
            if name == "default":
                cls.__client = None
                cls.__ws_client = None

    # -----------------------------------------------------------------------
    # Convenience helpers (unchanged public API)
    # -----------------------------------------------------------------------

    @classmethod
    async def reconnect(cls) -> HTTPConnection | None:
        """
        Reconnect to the SurrealDB instance.
        """
        await cls.close_connection("default")
        return await cls.get_client("default")

    @classmethod
    async def validate_connection(cls) -> bool:
        """
        Validate the connection to the SurrealDB instance.

        :return: True if the connection is valid, False otherwise.
        """
        try:
            await cls.reconnect()
            return True
        except SurrealDbConnectionError:
            return False

    @classmethod
    def get_connection_string(cls) -> str | None:
        """
        Get the connection string for the SurrealDB instance.

        :return: The connection string for the SurrealDB instance.
        """
        return cls.__url

    @classmethod
    def get_connection_kwargs(cls) -> dict[str, str | None]:
        """
        Get the connection kwargs for the SurrealDB instance.

        :return: The connection kwargs for the SurrealDB instance.
        """
        return {
            "url": cls.__url,
            "user": cls.__user,
            "namespace": cls.__namespace,
            "database": cls.__database,
        }

    @classmethod
    async def set_url(cls, url: str, reconnect: bool = False) -> bool:
        """
        Set the URL for the SurrealDB instance.

        :param url: The URL of the SurrealDB instance.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the URL when the others setting are not already set.")

        config = cls._configs["default"]
        cls.add_connection(
            "default",
            url=url,
            user=config.user,
            password=config.password,
            namespace=config.namespace,
            database=config.database,
            protocol=config.protocol,
        )

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls._configs.pop("default", None)
                cls._clear_legacy_vars()
                return False

        return True

    @classmethod
    async def set_user(cls, user: str, reconnect: bool = False) -> bool:
        """
        Set the username for authentication.

        :param user: The username for authentication.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the User when the others setting are not already set.")

        config = cls._configs["default"]
        cls.add_connection(
            "default",
            url=config.url,
            user=user,
            password=config.password,
            namespace=config.namespace,
            database=config.database,
            protocol=config.protocol,
        )

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls._configs.pop("default", None)
                cls._clear_legacy_vars()
                return False

        return True

    @classmethod
    async def set_password(cls, password: str, reconnect: bool = False) -> bool:
        """
        Set the password for authentication.

        :param password: The password for authentication.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the password when the others setting are not already set.")

        config = cls._configs["default"]
        cls.add_connection(
            "default",
            url=config.url,
            user=config.user,
            password=password,
            namespace=config.namespace,
            database=config.database,
            protocol=config.protocol,
        )

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls._configs.pop("default", None)
                cls._clear_legacy_vars()
                return False

        return True

    @classmethod
    async def set_namespace(cls, namespace: str, reconnect: bool = False) -> bool:
        """
        Set the namespace to use.

        :param namespace: The namespace to use.
        """

        if not cls.is_connection_set():
            raise ValueError("You can't change the namespace when the others setting are not already set.")

        config = cls._configs["default"]
        cls.add_connection(
            "default",
            url=config.url,
            user=config.user,
            password=config.password,
            namespace=namespace,
            database=config.database,
            protocol=config.protocol,
        )

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls._configs.pop("default", None)
                cls._clear_legacy_vars()
                return False

        return True

    @classmethod
    async def set_database(cls, database: str, reconnect: bool = False) -> bool:
        """
        Set the database to use.

        :param database: The database to use.
        """
        if not cls.is_connection_set():
            raise ValueError("You can't change the database when the others setting are not already set.")

        config = cls._configs["default"]
        cls.add_connection(
            "default",
            url=config.url,
            user=config.user,
            password=config.password,
            namespace=config.namespace,
            database=database,
            protocol=config.protocol,
        )

        if reconnect:
            if not await cls.validate_connection():  # pragma: no cover
                cls._configs.pop("default", None)
                cls._clear_legacy_vars()
                return False

        return True

    @classmethod
    def get_url(cls) -> str | None:
        """
        Get the URL of the SurrealDB instance.

        :return: The URL of the SurrealDB instance.
        """
        return cls.__url

    @classmethod
    def get_user(cls) -> str | None:
        """
        Get the username for authentication.

        :return: The username for authentication.
        """
        return cls.__user

    @classmethod
    def get_namespace(cls) -> str | None:
        """
        Get the namespace to use.

        :return: The namespace to use.
        """
        return cls.__namespace

    @classmethod
    def get_database(cls) -> str | None:
        """
        Get the database to use.

        :return: The database to use.
        """
        return cls.__database

    @classmethod
    def get_protocol(cls) -> Literal["json", "cbor"]:
        """
        Get the configured protocol (``"cbor"`` or ``"json"``).

        :return: The protocol string.
        """
        return cls.__protocol

    @classmethod
    def is_password_set(cls) -> bool:
        """
        Get the database to use.

        :return: The database to use.
        """
        return cls.__password is not None

    @classmethod
    def is_connected(cls) -> bool:
        """
        Check if the connection to the SurrealDB instance is established.

        :return: True if the connection is established, False otherwise.
        """

        return cls.__client is not None

    @classmethod
    async def call_function(
        cls,
        function: str,
        params: dict[str, Any] | None = None,
        return_type: type | None = None,
    ) -> Any:
        """
        Call a SurrealDB stored function.

        Delegates to the SDK's ``call()`` method, providing a convenient
        ORM-level API for invoking custom server-side functions defined
        with ``DEFINE FUNCTION fn::...``.

        Args:
            function: Function name (e.g., ``"acquire_game_lock"`` or
                ``"fn::acquire_game_lock"``). The ``fn::`` prefix is added
                automatically if not present.
            params: Named parameters to pass to the function.
            return_type: Optional Pydantic model or dataclass to convert
                the result to.

        Returns:
            The function return value, optionally converted to *return_type*.

        Example::

            result = await SurrealDBConnectionManager.call_function(
                "acquire_game_lock",
                params={"table_id": table_id, "pod_id": pod_id, "ttl": 30},
            )
        """
        client = await cls.get_client()
        return await client.call(function, params=params, return_type=return_type)

    @classmethod
    async def transaction(cls) -> HTTPTransaction:
        """
        Create a transaction context manager for atomic operations.

        Usage:
            async with await SurrealDBConnectionManager.transaction() as tx:
                user = User(name="Alice")
                await user.save(tx=tx)
                order = Order(user_id=user.id)
                await order.save(tx=tx)
                # Auto-commit on success, auto-rollback on exception

        :return: HTTPTransaction context manager
        """
        client = await cls.get_client()
        return client.transaction()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @classmethod
    def _sync_legacy_vars(cls, config: ConnectionConfig) -> None:
        """Keep the legacy class-level variables in sync with the default config."""
        cls.__url = config.url
        cls.__user = config.user
        cls.__password = config.password
        cls.__namespace = config.namespace
        cls.__database = config.database
        cls.__protocol = config.protocol

    @classmethod
    def _clear_legacy_vars(cls) -> None:
        """Reset all legacy class-level variables."""
        cls.__url = None
        cls.__user = None
        cls.__password = None
        cls.__namespace = None
        cls.__database = None
        cls.__protocol = "cbor"
        cls.__client = None
        cls.__ws_client = None

    @classmethod
    async def _close_single(cls, name: str) -> None:
        """Close HTTP and WS clients for a single named connection."""
        ws = cls._ws_clients.get(name)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.warning("Failed to close WebSocket connection '%s' cleanly.", name, exc_info=True)

        http = cls._clients.get(name)
        if http is not None:
            try:
                await http.close()
            except NotImplementedError:
                # Some HTTP client implementations may not support close();
                # ignore to maintain compatibility.
                logger.debug("HTTP client for connection '%s' does not implement close().", name)
