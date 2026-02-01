"""
SurrealDB SDK - A custom Python SDK for SurrealDB.

This SDK provides direct connection to SurrealDB via HTTP and WebSocket
without depending on the official surrealdb package.

Supports:
- HTTP connections (stateless, ideal for microservices)
- WebSocket connections (stateful, for real-time features)
- Live Queries (WebSocket only)
- Change Feeds streaming (HTTP, stateless)
"""

from typing import Any

from .connection.base import BaseSurrealConnection
from .connection.http import HTTPConnection
from .connection.websocket import WebSocketConnection
from .connection.pool import ConnectionPool
from .streaming.change_feed import ChangeFeedStream
from .streaming.live_query import LiveQuery
from .protocol.rpc import RPCRequest, RPCResponse, RPCError
from .types import (
    ResponseStatus,
    QueryResult,
    QueryResponse,
    RecordResponse,
    RecordsResponse,
    AuthResponse,
    InfoResponse,
    LiveQueryId,
    DeleteResponse,
)
from .exceptions import (
    SurrealDBError,
    ConnectionError,
    AuthenticationError,
    QueryError,
    TimeoutError,
)

__version__ = "0.1.0"
__all__ = [
    # Connections
    "BaseSurrealConnection",
    "HTTPConnection",
    "WebSocketConnection",
    "ConnectionPool",
    # Streaming
    "ChangeFeedStream",
    "LiveQuery",
    # Protocol
    "RPCRequest",
    "RPCResponse",
    "RPCError",
    # Response Types
    "ResponseStatus",
    "QueryResult",
    "QueryResponse",
    "RecordResponse",
    "RecordsResponse",
    "AuthResponse",
    "InfoResponse",
    "LiveQueryId",
    "DeleteResponse",
    # Exceptions
    "SurrealDBError",
    "ConnectionError",
    "AuthenticationError",
    "QueryError",
    "TimeoutError",
]


class SurrealDB:
    """
    Factory class for creating SurrealDB connections.

    Usage:
        # HTTP connection (stateless)
        async with SurrealDB.http("http://localhost:8000", "ns", "db") as db:
            await db.signin("root", "root")
            result = await db.query("SELECT * FROM users")

        # WebSocket connection (stateful)
        async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as db:
            await db.signin("root", "root")
            await db.live("users", callback=on_change)
    """

    @staticmethod
    def http(url: str, namespace: str, database: str, **kwargs: Any) -> HTTPConnection:
        """Create an HTTP connection (stateless)."""
        return HTTPConnection(url, namespace, database, **kwargs)

    @staticmethod
    def ws(url: str, namespace: str, database: str, **kwargs: Any) -> WebSocketConnection:
        """Create a WebSocket connection (stateful)."""
        return WebSocketConnection(url, namespace, database, **kwargs)

    @staticmethod
    def pool(url: str, namespace: str, database: str, size: int = 10, **kwargs: Any) -> ConnectionPool:
        """Create a connection pool."""
        return ConnectionPool(url, namespace, database, size=size, **kwargs)
