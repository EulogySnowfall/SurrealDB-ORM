"""Issue #604 — ``ConnectionPool`` forwards its keywords to HTTP connections too (v2 line).

Backport of #566 (fixed on ``main`` by #603).

``ConnectionPool(**kwargs)`` passed the extra keywords to ``WebSocketConnection``
only; an HTTP pool (the default) built every connection from url, namespace,
database and timeout, so ``protocol="json"`` was ignored and a keyword
``HTTPConnection`` does not take was dropped without an error.

Building a connection does not connect, and ``connect()`` is mocked where a
pool acquires one, so no server is needed.
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.surreal_sdk import SurrealDB
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.pool import ConnectionPool
from src.surreal_sdk.connection.websocket import WebSocketConnection

HTTP_URL = "http://localhost:8000"
WS_URL = "ws://localhost:8000"


class TestI604HTTPPoolForwardsKeywords:
    def test_protocol_json_reaches_http_connection(self) -> None:
        pool = ConnectionPool(HTTP_URL, "ns", "db", protocol="json")
        conn = pool._create_connection()
        assert isinstance(conn, HTTPConnection)
        assert conn.protocol == "json"

    def test_sdk_pool_factory_forwards_protocol(self) -> None:
        conn = SurrealDB.pool(HTTP_URL, "ns", "db", protocol="json")._create_connection()
        assert isinstance(conn, HTTPConnection)
        assert conn.protocol == "json"

    def test_default_protocol_unchanged(self) -> None:
        conn = ConnectionPool(HTTP_URL, "ns", "db")._create_connection()
        assert isinstance(conn, HTTPConnection)
        assert conn.protocol == "cbor"
        assert conn.timeout == 30.0

    def test_timeout_still_forwarded(self) -> None:
        conn = ConnectionPool(HTTP_URL, "ns", "db", timeout=7.5)._create_connection()
        assert conn.timeout == 7.5
        conn = ConnectionPool(HTTP_URL, "ns", "db", timeout=7.5, protocol="json")._create_connection()
        assert conn.timeout == 7.5
        assert conn.protocol == "json"

    async def test_acquire_yields_json_connection(self) -> None:
        pool = ConnectionPool(HTTP_URL, "ns", "db", size=1, protocol="json")
        with patch.object(HTTPConnection, "connect", new=AsyncMock()) as connect:
            async with pool.acquire() as conn:
                assert isinstance(conn, HTTPConnection)
                assert conn.protocol == "json"
            connect.assert_awaited_once()
        assert pool._semaphore._value == 1

    @pytest.mark.parametrize("keyword", ["max_message_size", "protocl"])
    def test_unknown_keyword_raises_on_create(self, keyword: str) -> None:
        pool = ConnectionPool(HTTP_URL, "ns", "db", **{keyword: 1024})
        with pytest.raises(TypeError, match=keyword):
            pool._create_connection()

    @pytest.mark.parametrize("keyword", ["max_message_size", "protocl"])
    async def test_unknown_keyword_raises_on_first_acquire(self, keyword: str) -> None:
        pool = ConnectionPool(HTTP_URL, "ns", "db", size=1, **{keyword: 1024})
        with patch.object(HTTPConnection, "connect", new=AsyncMock()) as connect:
            with pytest.raises(TypeError, match=keyword):
                async with pool.acquire():
                    pass  # pragma: no cover - never reached
            # No connection was built, so none was opened.
            connect.assert_not_awaited()
        # The failed acquire gave its slot back and tracked nothing.
        assert pool._semaphore._value == 1
        assert not pool._in_use
        assert not pool._pool
        assert not pool._pool_auth


class TestI604WebSocketPoolUnchanged:
    def test_websocket_keywords_still_forwarded(self) -> None:
        pool = ConnectionPool(
            WS_URL,
            "ns",
            "db",
            connection_type="websocket",
            timeout=4.0,
            protocol="json",
            max_message_size=2048,
        )
        conn = pool._create_connection()
        assert isinstance(conn, WebSocketConnection)
        assert conn.protocol == "json"
        assert conn.timeout == 4.0
        assert conn.max_message_size == 2048
