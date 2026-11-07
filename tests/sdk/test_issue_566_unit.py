"""Issue #566 — ``ConnectionPool`` forwards its keywords to HTTP connections too.

``ConnectionPool(**kwargs)`` passed the extra keywords to ``WebSocketConnection``
only; an HTTP pool (the default) built every connection from url, namespace,
database and timeout, so ``protocol="json"`` was ignored and a keyword
``HTTPConnection`` does not take was dropped without an error.

Building a connection does not connect, so no server is needed.
"""

import pytest

from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.pool import ConnectionPool
from src.surreal_sdk.connection.websocket import WebSocketConnection

HTTP_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000"


class TestI566HTTPPoolForwardsKeywords:
    def test_protocol_json_reaches_http_connection(self) -> None:
        pool = ConnectionPool(HTTP_URL, "ns", "db", protocol="json")
        conn = pool._create_connection()
        assert isinstance(conn, HTTPConnection)
        assert conn.protocol == "json"

    def test_default_protocol_unchanged(self) -> None:
        conn = ConnectionPool(HTTP_URL, "ns", "db")._create_connection()
        assert isinstance(conn, HTTPConnection)
        assert conn.protocol == "cbor"

    def test_timeout_still_forwarded(self) -> None:
        conn = ConnectionPool(HTTP_URL, "ns", "db", timeout=7.5, protocol="json")._create_connection()
        assert conn.timeout == 7.5

    def test_unknown_keyword_raises_on_create(self) -> None:
        pool = ConnectionPool(HTTP_URL, "ns", "db", max_message_size=1024)
        with pytest.raises(TypeError, match="max_message_size"):
            pool._create_connection()

    async def test_unknown_keyword_raises_on_first_acquire(self) -> None:
        pool = ConnectionPool(HTTP_URL, "ns", "db", size=1, max_message_size=1024)
        with pytest.raises(TypeError, match="max_message_size"):
            async with pool.acquire():
                pass  # pragma: no cover - never reached
        # The failed acquire gave its slot back and tracked nothing.
        assert pool._semaphore._value == 1
        assert not pool._in_use
        assert not pool._pool


class TestI566WebSocketPoolUnchanged:
    def test_websocket_keywords_still_forwarded(self) -> None:
        pool = ConnectionPool(
            WS_URL,
            "ns",
            "db",
            connection_type="websocket",
            protocol="json",
            max_message_size=2048,
        )
        conn = pool._create_connection()
        assert isinstance(conn, WebSocketConnection)
        assert conn.protocol == "json"
        assert conn.max_message_size == 2048
