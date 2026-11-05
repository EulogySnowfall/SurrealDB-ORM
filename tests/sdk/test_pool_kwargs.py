"""
Unit tests: ``ConnectionPool`` forwards its extra keywords to every connection.

The HTTP branch of ``_create_connection()`` used to build its connections from
url, namespace, database and timeout only, so ``protocol="json"`` was ignored
(CBOR was used) and any other keyword was dropped without an error. No server
is needed: ``_create_connection()`` only builds the connection object.
"""

import pytest

from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.pool import ConnectionPool
from surreal_sdk.connection.websocket import WebSocketConnection

URL = "http://localhost:8000"
NS = "test"
DB = "test"


class TestHTTPPoolForwardsKeywords:
    """An HTTP pool (the default ``connection_type``) forwards ``**kwargs``."""

    def test_protocol_json_reaches_http_connection(self) -> None:
        pool = ConnectionPool(URL, NS, DB, protocol="json")
        conn = pool._create_connection()
        assert isinstance(conn, HTTPConnection)
        assert conn.protocol == "json"

    def test_explicit_http_type_forwards_protocol(self) -> None:
        pool = ConnectionPool(URL, NS, DB, connection_type="http", protocol="json")
        conn = pool._create_connection()
        assert isinstance(conn, HTTPConnection)
        assert conn.protocol == "json"

    def test_default_protocol_is_still_cbor(self) -> None:
        conn = ConnectionPool(URL, NS, DB)._create_connection()
        assert isinstance(conn, HTTPConnection)
        assert conn.protocol == "cbor"

    def test_timeout_still_reaches_http_connection(self) -> None:
        conn = ConnectionPool(URL, NS, DB, timeout=7.5, protocol="json")._create_connection()
        assert conn.timeout == 7.5

    def test_websocket_only_keyword_raises_type_error(self) -> None:
        pool = ConnectionPool(URL, NS, DB, max_message_size=1024)
        with pytest.raises(TypeError, match="max_message_size"):
            pool._create_connection()

    async def test_websocket_only_keyword_raises_at_first_acquire(self) -> None:
        pool = ConnectionPool(URL, NS, DB, size=1, max_message_size=1024)
        with pytest.raises(TypeError, match="max_message_size"):
            async with pool.acquire():
                pass  # pragma: no cover - never reached
        # The failed creation must not leak a slot: the pool stays usable.
        assert pool._semaphore._value == 1
        assert not pool._in_use
        await pool.close()


class TestWebSocketPoolUnchanged:
    """A WebSocket pool already forwarded its keywords; it still does."""

    def test_protocol_json_reaches_websocket_connection(self) -> None:
        pool = ConnectionPool("ws://localhost:8000", NS, DB, connection_type="websocket", protocol="json")
        conn = pool._create_connection()
        assert isinstance(conn, WebSocketConnection)
        assert conn.protocol == "json"
        assert conn.timeout == 30.0

    def test_max_message_size_reaches_websocket_connection(self) -> None:
        pool = ConnectionPool("ws://localhost:8000", NS, DB, connection_type="websocket", max_message_size=1024)
        conn = pool._create_connection()
        assert isinstance(conn, WebSocketConnection)
        assert conn.max_message_size == 1024
