"""
SurrealDB SDK Connection Module.

Provides HTTP and WebSocket connection implementations.
"""

from .base import BaseSurrealConnection
from .http import HTTPConnection
from .pool import ConnectionPool
from .websocket import WebSocketConnection

__all__ = [
    "BaseSurrealConnection",
    "ConnectionPool",
    "HTTPConnection",
    "WebSocketConnection",
]
