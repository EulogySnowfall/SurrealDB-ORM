"""
SurrealDB SDK Connection Module.

Provides HTTP and WebSocket connection implementations.
"""

from .base import BaseSurrealConnection
from .http import HTTPConnection
from .websocket import WebSocketConnection
from .pool import ConnectionPool

__all__ = [
    "BaseSurrealConnection",
    "HTTPConnection",
    "WebSocketConnection",
    "ConnectionPool",
]
