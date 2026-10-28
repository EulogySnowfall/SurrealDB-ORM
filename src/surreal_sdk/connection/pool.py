"""
Connection Pool Implementation for SurrealDB SDK.

Provides connection pooling for both HTTP and WebSocket connections.
"""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, Self

from ..exceptions import ConnectionError
from ..protocol.rpc import RPCMethod
from ..types import DeleteResponse, QueryResponse, RecordResponse, RecordsResponse
from .base import BaseSurrealConnection
from .http import HTTPConnection
from .websocket import WebSocketConnection


class ConnectionPool:
    """
    Connection pool for SurrealDB connections.

    Manages a pool of reusable connections for improved performance
    in high-throughput scenarios.
    """

    def __init__(
        self,
        url: str,
        namespace: str,
        database: str,
        size: int = 10,
        connection_type: str = "http",
        timeout: float = 30.0,
        **kwargs: Any,
    ):
        """
        Initialize connection pool.

        Args:
            url: SurrealDB server URL
            namespace: Target namespace
            database: Target database
            size: Maximum pool size
            connection_type: "http" or "websocket"
            timeout: Connection timeout in seconds
            **kwargs: Additional connection arguments
        """
        if size <= 0:
            raise ValueError(f"Pool size must be > 0, got {size}")

        self.url = url
        self.namespace = namespace
        self.database = database
        self.size = size
        self.connection_type = connection_type
        self.timeout = timeout
        self.kwargs = kwargs

        self._pool: deque[BaseSurrealConnection] = deque()
        self._in_use: set[BaseSurrealConnection] = set()
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(size)
        self._closed = False
        self._credentials: tuple[str, str] | None = None

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    def _create_connection(self) -> BaseSurrealConnection:
        """Create a new connection instance."""
        if self.connection_type == "websocket":
            return WebSocketConnection(
                self.url,
                self.namespace,
                self.database,
                timeout=self.timeout,
                **self.kwargs,
            )
        else:
            return HTTPConnection(
                self.url,
                self.namespace,
                self.database,
                timeout=self.timeout,
            )

    async def _init_connection(self, conn: BaseSurrealConnection) -> None:
        """Initialize a connection."""
        await conn.connect()
        if self._credentials:
            user, password = self._credentials
            await conn.signin(user, password)

    async def set_credentials(self, user: str, password: str) -> None:
        """
        Set credentials for all pool connections.

        Idle connections are signed in again with the new credentials; one that
        fails to sign in is closed and dropped rather than kept with its
        previous identity. Connections in use are signed in with the new
        credentials when they are released (see ``acquire()``).

        Args:
            user: Username
            password: Password
        """
        self._credentials = (user, password)

        # Re-authenticate idle connections
        async with self._lock:
            kept: deque[BaseSurrealConnection] = deque()
            while self._pool:
                conn = self._pool.popleft()
                if await self._reset_auth(conn):
                    kept.append(conn)
                else:
                    await self._close_quietly(conn)
            self._pool = kept

    async def _reset_auth(self, conn: BaseSurrealConnection) -> bool:
        """
        Return *conn* to the pool's own authentication state.

        A caller holding a pooled connection may change its identity
        (``signin()``, ``signup()``, ``authenticate()``, ``invalidate()``); the
        next caller must not inherit it. With credentials, the connection is
        signed in again with them; without, its session is invalidated so it
        comes back unauthenticated. The token a ``WebSocketConnection`` replays
        on reconnect is updated to match.

        Returns:
            True when the reset succeeded and the connection may be reused;
            False when it failed and the connection must be closed instead.
        """
        try:
            if self._credentials:
                user, password = self._credentials
                response = await conn.signin(user, password)
                # ``signin()`` keeps the previous token when the server returns
                # none; the token replayed on reconnect must be the pool's.
                conn._token = response.token
            elif isinstance(conn, HTTPConnection):
                await conn.invalidate()
            else:
                try:
                    await conn.rpc(RPCMethod.INVALIDATE)
                finally:
                    conn._token = None
                    conn._authenticated = False
        except Exception:
            return False
        return conn.is_connected

    @staticmethod
    async def _close_quietly(conn: BaseSurrealConnection) -> None:
        """Close *conn*, ignoring errors (it is being discarded)."""
        try:
            await conn.close()
        except Exception:
            pass  # Discarded connection; nothing left to do with the error

    @asynccontextmanager
    async def acquire(self) -> AsyncGenerator[BaseSurrealConnection, None]:
        """
        Acquire a connection from the pool.

        Usage:
            async with pool.acquire() as conn:
                result = await conn.query("SELECT * FROM users")

        On release the connection is returned to the pool's own
        authentication state (signed in again with the pool's credentials, or
        invalidated when the pool has none), so an identity set by the caller
        is never handed to the next one. A connection whose reset fails is
        closed instead of being reused.

        Yields:
            A SurrealDB connection
        """
        if self._closed:
            raise ConnectionError("Pool is closed")

        conn: BaseSurrealConnection | None = None

        await self._semaphore.acquire()
        try:
            async with self._lock:
                # Try to get an existing connection from pool
                while self._pool:
                    conn = self._pool.popleft()
                    if conn.is_connected:
                        break
                    # Connection is dead, discard it
                    try:
                        await conn.close()
                    except Exception:
                        pass  # Dead connection; discard silently
                    conn = None

                # Create new connection if needed
                if conn is None:
                    if len(self._in_use) < self.size:
                        conn = self._create_connection()
                        await self._init_connection(conn)
                    else:
                        # Should not happen with semaphore, but just in case
                        raise RuntimeError("No connection available")

                self._in_use.add(conn)
        except BaseException:
            self._semaphore.release()
            raise

        try:
            yield conn
        finally:
            # Reset the identity the caller may have left on the connection
            # before anyone else can acquire it. Done outside the lock so a
            # slow reset does not stall other acquisitions; the connection
            # stays in ``_in_use`` meanwhile, so ``close()`` still sees it.
            reusable = False
            try:
                if not self._closed and conn.is_connected:
                    reusable = await self._reset_auth(conn)
            finally:
                async with self._lock:
                    self._in_use.discard(conn)
                    if reusable and not self._closed and conn.is_connected:
                        self._pool.append(conn)
                    else:
                        await self._close_quietly(conn)
                self._semaphore.release()

    async def close(self) -> None:
        """Close all connections in the pool."""
        self._closed = True

        async with self._lock:
            # Close pooled connections
            while self._pool:
                conn = self._pool.popleft()
                try:
                    await conn.close()
                except Exception:
                    pass

            # Close in-use connections
            for conn in self._in_use:
                try:
                    await conn.close()
                except Exception:
                    pass
            self._in_use.clear()

    @property
    def available(self) -> int:
        """Number of available connections in pool."""
        return len(self._pool)

    @property
    def in_use(self) -> int:
        """Number of connections currently in use."""
        return len(self._in_use)

    @property
    def total(self) -> int:
        """Total number of connections (available + in use)."""
        return len(self._pool) + len(self._in_use)

    # Convenience methods that acquire a connection

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        """Execute a query using a pooled connection."""
        async with self.acquire() as conn:
            return await conn.query(sql, vars)

    async def select(self, thing: str) -> RecordsResponse:
        """Select records using a pooled connection."""
        async with self.acquire() as conn:
            return await conn.select(thing)

    async def create(self, thing: str, data: dict[str, Any] | None = None) -> RecordResponse:
        """Create a record using a pooled connection."""
        async with self.acquire() as conn:
            return await conn.create(thing, data)

    async def update(self, thing: str, data: dict[str, Any]) -> RecordsResponse:
        """Update record(s) using a pooled connection."""
        async with self.acquire() as conn:
            return await conn.update(thing, data)

    async def merge(self, thing: str, data: dict[str, Any]) -> RecordsResponse:
        """Merge data into record(s) using a pooled connection."""
        async with self.acquire() as conn:
            return await conn.merge(thing, data)

    async def delete(self, thing: str) -> DeleteResponse:
        """Delete record(s) using a pooled connection."""
        async with self.acquire() as conn:
            return await conn.delete(thing)
