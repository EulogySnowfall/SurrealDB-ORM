"""
SurrealDB SDK - A custom Python SDK for SurrealDB.

This SDK provides direct connection to SurrealDB via HTTP and WebSocket
without depending on the official surrealdb package.

Supports:
- HTTP connections (stateless, ideal for microservices)
- WebSocket connections (stateful, for real-time features)
- Live Queries (WebSocket only)
- Live Select Streams (async iterator pattern)
- Change Feeds streaming (HTTP, stateless)
- Typed function calls
"""

from typing import Any

from .connection.base import BaseSurrealConnection
from .connection.http import HTTPConnection
from .connection.websocket import WebSocketConnection
from .connection.pool import ConnectionPool
from .streaming.change_feed import ChangeFeedStream
from .streaming.live_query import LiveQuery, LiveQueryManager, LiveNotification
from .streaming.live_select import (
    LiveSelectStream,
    LiveSelectManager,
    LiveChange,
    LiveAction,
    LiveSubscriptionParams,
)
from .protocol.rpc import RPCRequest, RPCResponse, RPCError
from .protocol.cbor import (
    CBOR_AVAILABLE,
    RecordId,
    Table,
    Duration,
    is_available as cbor_is_available,
)
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
    TransactionError,
    TransactionConflictError,
)
from .transaction import (
    BaseTransaction,
    HTTPTransaction,
    WebSocketTransaction,
    TransactionStatement,
)
from .functions import (
    FunctionCall,
    FunctionNamespace,
    MathFunctions,
    TimeFunctions,
    ArrayFunctions,
    StringFunctions,
    CryptoFunctions,
)

__version__ = "0.5.9"
__all__ = [
    # Connections
    "BaseSurrealConnection",
    "HTTPConnection",
    "WebSocketConnection",
    "ConnectionPool",
    # Streaming - Live Query (callback-based)
    "ChangeFeedStream",
    "LiveQuery",
    "LiveQueryManager",
    "LiveNotification",
    # Streaming - Live Select (async iterator)
    "LiveSelectStream",
    "LiveSelectManager",
    "LiveChange",
    "LiveAction",
    "LiveSubscriptionParams",
    # Protocol
    "RPCRequest",
    "RPCResponse",
    "RPCError",
    # CBOR Types
    "CBOR_AVAILABLE",
    "RecordId",
    "Table",
    "Duration",
    "cbor_is_available",
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
    # Transactions
    "BaseTransaction",
    "HTTPTransaction",
    "WebSocketTransaction",
    "TransactionStatement",
    # Functions
    "FunctionCall",
    "FunctionNamespace",
    "MathFunctions",
    "TimeFunctions",
    "ArrayFunctions",
    "StringFunctions",
    "CryptoFunctions",
    # Exceptions
    "SurrealDBError",
    "ConnectionError",
    "AuthenticationError",
    "QueryError",
    "TimeoutError",
    "TransactionError",
    "TransactionConflictError",
]


class SurrealDB:
    """
    Factory class for creating SurrealDB connections.

    Usage:
        # HTTP connection (stateless)
        async with SurrealDB.http("http://localhost:8000", "ns", "db") as db:
            await db.signin("root", "root")
            result = await db.query("SELECT * FROM users")

        # WebSocket connection (stateful, CBOR protocol - default)
        # CBOR properly handles strings like 'data:image/png;base64,...' that
        # JSON would incorrectly interpret as record links.
        async with SurrealDB.ws("ws://localhost:8000", "ns", "db") as db:
            await db.signin("root", "root")
            await db.live("users", callback=on_change)
            # data:xxx values are handled correctly with CBOR

        # WebSocket connection with JSON protocol (for debugging/compatibility)
        async with SurrealDB.ws("ws://localhost:8000", "ns", "db", protocol="json") as db:
            await db.signin("root", "root")
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
