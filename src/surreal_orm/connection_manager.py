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
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal

from surreal_sdk import HTTPConnection, WebSocketConnection
from surreal_sdk.connection.http import DumpDestination, DumpSource
from surreal_sdk.exceptions import SurrealDBError
from surreal_sdk.transaction import HTTPTransaction
from surreal_sdk.utils import validate_table_name

from .connection_config import ConnectionConfig

if TYPE_CHECKING:
    from .model_base import BaseSurrealModel

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
    _ws_connection_lock: asyncio.Lock | None = None

    # Upper bound, in seconds, that ``reconnect()`` waits for requests still in
    # flight on the client it replaces before closing that client anyway. It
    # matches ``HTTPConnection``'s default request timeout.
    _reconnect_drain_timeout: float = 30.0

    # Closes of half-built clients that outlived a second cancellation; kept
    # referenced until they finish (the event loop holds tasks only weakly).
    _background_closes: set[asyncio.Future[None]] = set()

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
        try:
            await cls._close_single(name)
        finally:
            # Unregister even if closing failed, so a dead client is never reused.
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

        async with cls._get_connection_lock():
            # Double-check after acquiring the lock (another coroutine may
            # have created the connection while we were waiting).
            existing = cls._clients.get(name)
            if existing is not None and existing.is_connected:
                return existing

            _client = await cls._build_client(name)
            cls._register_client(name, _client)
            return _client

    @classmethod
    async def get_ws_client(cls, name: str | None = None) -> WebSocketConnection:
        """
        Get or create a WebSocket connection to SurrealDB.

        The WebSocket connection is created lazily on first call and reused
        for subsequent calls. It uses the same URL and credentials as the
        HTTP connection, with automatic ``http`` → ``ws`` URL conversion
        (handled by ``WebSocketConnection.__init__``).

        Required for Live Queries (``QuerySet.live()``).

        Concurrent first calls create one client: the client is built,
        connected, signed in and registered under a lock of its own (not the
        HTTP client lock), and every caller that waited on it gets the client
        the first one registered.

        Args:
            name: Connection name.  ``None`` means use the active connection.

        :return: The WebSocketConnection instance.
        :raises ValueError: If connection settings have not been configured.
        :raises SurrealDbConnectionError: If the WebSocket connection fails.
        """
        name = name or cls.get_active_connection_name() or "default"

        # Fast path: reuse existing connected client
        existing = cls._ws_clients.get(name)
        if existing is not None and existing.is_connected:
            return existing

        async with cls._get_ws_connection_lock():
            # Double-check after acquiring the lock (another coroutine may
            # have created the connection while we were waiting).
            existing = cls._ws_clients.get(name)
            if existing is not None and existing.is_connected:
                return existing
            return await cls._build_ws_client(name)

    @classmethod
    async def _build_ws_client(cls, name: str) -> WebSocketConnection:
        """Create, connect, authenticate and register the WebSocket client of *name*.

        Called by ``get_ws_client()`` with the WebSocket client lock held.
        """
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

            # SurrealDB 3.0: ensure namespace/database exist.
            try:
                await _ws_client.query(
                    f"DEFINE NAMESPACE IF NOT EXISTS `{config.namespace}`; DEFINE DATABASE IF NOT EXISTS `{config.database}`;"
                )
            except SurrealDBError:
                logger.debug(
                    "Could not auto-create namespace/database (WS) for '%s'.",
                    name,
                )

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
        # The registry is cleared in ``finally`` so that a client whose close
        # failed is never handed out again by ``get_client()``.
        if name is None:
            # Close all connections
            names = list(cls._clients.keys()) + list(cls._ws_clients.keys())
            try:
                for n in set(names):
                    await cls._close_single(n)
            finally:
                cls._clients.clear()
                cls._ws_clients.clear()
                cls.__client = None
                cls.__ws_client = None
        else:
            try:
                await cls._close_single(name)
            finally:
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
        Replace the default HTTP client with a newly connected and authenticated one.

        The new client is built and signed in first, then swapped in under the
        connection lock, so concurrent callers of ``get_client()`` always get a
        connected client. RPC requests already running on the previous client
        (``query()``, ``select()``, ``create()`` and the other RPC methods;
        not ``sql()``, ``health()``, ``status()`` or the ``rest_*()`` calls,
        which are not counted), and transactions already open on it (from
        ``transaction()``, which send their statements at commit), are allowed
        to finish: it is closed once none is left, or after
        ``_reconnect_drain_timeout`` seconds (30 by default), whichever comes
        first, and this call returns after that close. Transactions opened by
        the task calling ``reconnect()`` are not waited for (they cannot end
        while it waits): the previous client is then closed when the last of
        them commits or rolls back. The default WebSocket client, if any, is
        closed as before and recreated on the next ``get_ws_client()``.

        Call ``reconnect()`` from the task that opened a transaction, not from
        a task that the transaction awaits (``asyncio.gather()``, a
        ``TaskGroup``): that task is not the transaction's own, so the drain
        waits for the transaction while the transaction waits for the drain,
        until the timeout closes the previous client and the commit fails.

        A ``QuerySet.changes()`` stream (a ``ChangeModelStream`` built without
        ``connection=``) moves to the new client and resumes from its cursor.

        Code that keeps a client from ``get_client()`` across an ``await``
        and uses it after a ``reconnect()`` gets ``Not connected`` once that
        client is closed: call ``get_client()`` again instead of holding it.

        If the new client cannot be connected or authenticated,
        ``SurrealDbConnectionError`` is raised and the current clients are
        left in place.

        Cancelling the call (for example ``asyncio.wait_for(reconnect(), 5)``
        timing out) does not close the previous client under the requests and
        transactions still using it. Cancelled before the swap, it leaves the
        current clients in place. Cancelled after it, the new client stays
        installed and the previous one is closed by the last RPC request or
        transaction to finish on it (at once if none is left); the
        cancellation then propagates as usual. A client still being built when
        the call is cancelled is closed before the cancellation propagates.

        Token renewal does not need this method: SurrealDB answers a request
        made with an expired token with HTTP 401, and the HTTP client signs in
        again with its stored credentials and retries the request.

        :return: The new default HTTP client.
        :raises ValueError: If the default connection is not configured.
        :raises SurrealDbConnectionError: If the new client cannot connect.
        """
        name = "default"
        async with cls._get_connection_lock():
            new_client = await cls._build_client(name)
            old_client = cls._clients.get(name)
            old_ws = cls._ws_clients.pop(name, None)
            cls.__ws_client = None
            cls._register_client(name, new_client)

        # Retire the previous clients outside the lock, so that callers needing
        # a client are not held up while in-flight requests drain.
        try:
            await cls._close_clients(name, old_client, old_ws, drain_timeout=cls._reconnect_drain_timeout)
        except asyncio.CancelledError:
            # Cancelled before the previous HTTP client was retired (e.g. while
            # the WebSocket client closed): its last user closes it instead.
            # A cancelled drain has already done this; doing it again is a no-op.
            if old_client is not None:
                await old_client._close_when_idle()
            raise
        return new_client

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
    # Database dump / restore
    # -----------------------------------------------------------------------

    @classmethod
    async def dump_database(
        cls,
        dest: DumpDestination,
        *,
        connection: str | None = None,
        tables: Sequence[str | type[BaseSurrealModel]] | None = None,
        users: bool = True,
        accesses: bool = True,
        params: bool = True,
        functions: bool = True,
        analyzers: bool = True,
        records: bool = True,
    ) -> int:
        """
        Write a SurrealQL dump of a connection's database to ``dest``.

        The dump is the server's own format (the one ``surreal export``
        writes): ``OPTION IMPORT;``, the definitions, then the rows as
        ``INSERT`` statements. It keeps every value as the server stores it
        (geometry, nanosecond datetimes, ``NULL``), which JSON Lines cannot.
        Delegates to :meth:`HTTPConnection.export_database` on the resolved
        connection; the transfer is streamed and has no client timeout.

        **A dump contains every user's password hash and the access
        definitions (including JWT keys)** unless users and accesses are
        excluded: ``dump_database(path, users=False, accesses=False)``, or
        ``surreal-orm dump --no-users --no-accesses -o app.surql``. A **new**
        file is created with mode ``0o600`` (readable by its owner only); an
        existing file is truncated and keeps its mode.

        Example::

            await SurrealDBConnectionManager.dump_database("backup.surql")
            await SurrealDBConnectionManager.dump_database(
                "posts.surql", tables=[Post, "audit"], users=False, accesses=False
            )

        Args:
            dest: A path (created or truncated) or a binary stream, which is
                written to and left open.
            connection: Connection name; ``None`` uses the active one
                (``using()`` context, then ``"default"``).
            tables: Tables to dump, as names or model classes (read through
                ``get_table_name()``); ``None`` (default) dumps every table.
            users: Include ``DEFINE USER`` statements (password hashes).
            accesses: Include ``DEFINE ACCESS`` statements (keys, record
                access logic).
            params: Include ``DEFINE PARAM`` statements.
            functions: Include ``DEFINE FUNCTION`` statements.
            analyzers: Include ``DEFINE ANALYZER`` statements.
            records: Include the tables' rows; ``False`` dumps definitions
                only.

        Returns:
            int: The number of bytes written.

        Raises:
            ValueError: Before any request, if a table name is not an
                identifier (``Invalid table name: …``).
            TypeError: Before any request, if ``tables`` is a single string.
            ConnectionError, QueryError, OSError: As
                :meth:`HTTPConnection.export_database`.
        """
        names = cls._dump_table_names(tables)
        client = await cls.get_client(connection)
        return await client.export_database(
            dest,
            tables=names,
            users=users,
            accesses=accesses,
            params=params,
            functions=functions,
            analyzers=analyzers,
            records=records,
        )

    @staticmethod
    def _dump_table_names(tables: Sequence[str | type[BaseSurrealModel]] | None) -> list[str] | None:
        """The validated table names of a ``dump_database(tables=...)`` argument (models read by ``get_table_name()``)."""
        if tables is None:
            return None
        if isinstance(tables, str):
            raise TypeError("tables must be a sequence of table names or model classes, not a str")
        return [validate_table_name(entry if isinstance(entry, str) else entry.get_table_name()) for entry in tables]

    @classmethod
    async def restore_database(cls, source: DumpSource, *, connection: str | None = None) -> None:
        """
        Restore a SurrealQL dump into a connection's database.

        Delegates to :meth:`HTTPConnection.import_database` on the resolved
        connection; ``source`` is a path (a ``str`` is always a path), the
        dump's ``bytes``, a binary stream or an async iterable of ``bytes``,
        streamed to the server. The database is created when the ORM
        connects, so it exists.

        What a restore does, as measured on SurrealDB 3.2 and 3.3:

        - It runs under the dump's ``OPTION IMPORT;``: no field processing,
          no ``ASSERT``/``VALUE``, no events, **no ORM signal**. It needs a
          system user (root, namespace or database ``OWNER``).
        - A definition of the same name (table, field, index, event,
          function, param) is **replaced** by the dump's; a definition only
          in the target **stays**. A target where migrations were applied
          restores cleanly as long as the dumped tables hold no rows.
        - It is **not transactional**: a row that already exists fails its
          own ``INSERT`` statement (``DataImportError``) and the other
          statements, other tables included, are applied.

        The whole :class:`QueryCache` is cleared afterwards, also when the
        restore fails, since any table may have changed.

        Example::

            async with SurrealDBConnectionManager.using("restore_target"):
                await SurrealDBConnectionManager.restore_database("backup.surql")

        Args:
            source: The dump to restore.
            connection: Connection name; ``None`` uses the active one
                (``using()`` context, then ``"default"``).

        Raises:
            DataImportError: If the server rejects statements, answers with
                an error status or closes the connection during the upload
                (see :meth:`HTTPConnection.import_database`).
            TypeError, ConnectionError, OSError: As
                :meth:`HTTPConnection.import_database`.
        """
        from .cache import QueryCache

        try:
            client = await cls.get_client(connection)
            await client.import_database(source)
        finally:
            QueryCache.clear()

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
    def _get_connection_lock(cls) -> asyncio.Lock:
        """Return the lock that serialises creating and swapping HTTP clients."""
        if cls._connection_lock is None:
            cls._connection_lock = asyncio.Lock()
        return cls._connection_lock

    @classmethod
    def _get_ws_connection_lock(cls) -> asyncio.Lock:
        """Return the lock that serialises creating WebSocket clients.

        Separate from ``_get_connection_lock()``: ``get_client()`` holds that
        lock while it builds an HTTP client, and a WebSocket build must not
        wait for it.
        """
        if cls._ws_connection_lock is None:
            cls._ws_connection_lock = asyncio.Lock()
        return cls._ws_connection_lock

    @classmethod
    async def _build_client(cls, name: str) -> HTTPConnection:
        """Create, connect and authenticate a new HTTP client for connection *name*.

        The registry is not modified: the caller installs the client with
        ``_register_client()``.

        :raises ValueError: If *name* is not configured.
        :raises SurrealDbConnectionError: If the client cannot connect or
            authenticate. The partly built client is closed first.
        :raises asyncio.CancelledError: If the call is cancelled (for example
            ``asyncio.wait_for(reconnect(), 5)`` expiring during sign-in). The
            partly built client is closed first, and the cancellation is
            re-raised unchanged.
        """
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

            # SurrealDB 3.0: namespaces/databases must exist before use.
            # Ensure they are created after root signin.
            try:
                await _client.query(
                    f"DEFINE NAMESPACE IF NOT EXISTS `{config.namespace}`; DEFINE DATABASE IF NOT EXISTS `{config.database}`;"
                )
            except SurrealDBError:
                logger.debug(
                    "Could not auto-create namespace/database for '%s' (may lack root privileges).",
                    name,
                )

            return _client
        except asyncio.CancelledError:
            # Not an Exception: without this, the client would keep its open
            # httpx client with nothing referring to it.
            if _client is not None:
                await cls._close_cancelled_build(_client)
            raise
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
    async def _close_cancelled_build(cls, client: HTTPConnection) -> None:
        """Close a client whose build was cancelled, without raising.

        The close runs shielded, so a second cancellation while it runs does
        not interrupt it: it finishes in the background instead. A failure is
        logged. The caller re-raises its own cancellation afterwards.
        """
        close = asyncio.ensure_future(client._close_quietly())
        cls._background_closes.add(close)
        close.add_done_callback(cls._background_closes.discard)
        try:
            await asyncio.shield(close)
        except asyncio.CancelledError:
            pass  # cancelled again: the close goes on, the first cancellation is re-raised

    @classmethod
    def _register_client(cls, name: str, client: HTTPConnection) -> None:
        """Install *client* as the HTTP client of connection *name*."""
        cls._clients[name] = client
        # Keep legacy alias in sync
        if name == "default":
            cls.__client = client

    @classmethod
    async def _close_single(cls, name: str) -> None:
        """Close HTTP and WS clients for a single named connection."""
        await cls._close_clients(name, cls._clients.get(name), cls._ws_clients.get(name))

    @classmethod
    async def _close_clients(
        cls,
        name: str,
        http: HTTPConnection | None,
        ws: WebSocketConnection | None,
        *,
        drain_timeout: float | None = None,
    ) -> None:
        """Close the given clients of connection *name*, logging failures instead of raising.

        Args:
            name: Connection name, for log messages.
            http: HTTP client to close, if any.
            ws: WebSocket client to close, if any.
            drain_timeout: When set, the HTTP client is closed only once no
                request or transaction is in flight on it, or after this many
                seconds (see ``HTTPConnection._drain_and_close``).
        """
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.warning("Failed to close WebSocket connection '%s' cleanly.", name, exc_info=True)

        if http is not None:
            try:
                if drain_timeout is None:
                    await http.close()
                elif not await http._drain_and_close(drain_timeout):
                    logger.warning(
                        "Retired the previous HTTP client for connection '%s' with requests or transactions "
                        "still in flight after %.1f s.",
                        name,
                        drain_timeout,
                    )
            except NotImplementedError:
                # Some HTTP client implementations may not support close();
                # ignore to maintain compatibility.
                logger.debug("HTTP client for connection '%s' does not implement close().", name)
            except Exception:
                # E.g. ``RuntimeError: Event loop is closed`` for a client bound
                # to a loop that no longer runs. The caller still unregisters it.
                logger.warning("Failed to close HTTP connection '%s' cleanly.", name, exc_info=True)
