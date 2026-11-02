"""SDK fixes carried by v0.37.0, against the pinned server.

I13: a ``Duration`` bound as a query parameter reaches the server as a duration,
over HTTP and over WebSocket. A string ``Duration`` used to be sent as CBOR tag 14
(the compact array form), which the server cannot read: HTTP answered
``400 Parse error`` for the whole request and WebSocket never replied.

I12: a WebSocket reply above 4 MiB arrives. aiohttp caps a received message at
4 MiB unless ``ws_connect()`` says otherwise; a larger reply dropped the socket
and the request waited for its whole timeout. With an explicit
``max_message_size`` the request fails at once, and the connection serves the
next query after its reconnect.
"""

import asyncio
import time
from collections.abc import AsyncGenerator

import pytest

from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.exceptions import ConnectionError
from surreal_sdk.protocol.cbor import Duration

NS = "test"
DB = "dwf_duration_ws"
BIG_TABLE = "dwf_big"
MiB = 1024 * 1024


@pytest.fixture(params=["http", "ws"])
async def conn(request: pytest.FixtureRequest) -> AsyncGenerator[BaseSurrealConnection, None]:
    """A signed-in CBOR connection on each transport (WebSocket with a short timeout, so a hang fails fast)."""
    connection: BaseSurrealConnection
    if request.param == "http":
        connection = HTTPConnection("http://localhost:8000", NS, DB)
    else:
        connection = WebSocketConnection("ws://localhost:8000", NS, DB, timeout=5, auto_reconnect=False)
    try:
        await connection.connect()
        await connection.signin("root", "root")
        await connection.query(f"DEFINE NAMESPACE IF NOT EXISTS {NS}; DEFINE DATABASE IF NOT EXISTS {DB};")
        yield connection
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_string_duration_binds_as_duration(conn: BaseSurrealConnection) -> None:
    response = await conn.query("RETURN [type::of($d), <string>$d]", {"d": Duration("1h30m")})
    assert response.first_result.result == ["duration", "1h30m"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_compact_duration_binds_as_duration(conn: BaseSurrealConnection) -> None:
    response = await conn.query("RETURN [type::of($d), <string>$d]", {"d": Duration((5400, 1))})
    assert response.first_result.result == ["duration", "1h30m1ns"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_duration_read_back_rebinds_equal(conn: BaseSurrealConnection) -> None:
    read = await conn.query("RETURN 1h30m1ns")
    value = read.first_result.result
    assert isinstance(value, Duration)
    assert str(value) == "5400s1ns"

    response = await conn.query("RETURN [$d = 1h30m1ns, $s = 1h30m1ns]", {"d": value, "s": Duration(str(value))})
    assert response.first_result.result == [True, True]


@pytest.fixture(scope="module")
async def big_rows() -> AsyncGenerator[None, None]:
    """Six rows of 1 MB each, written over HTTP one statement at a time."""
    http = HTTPConnection("http://localhost:8000", NS, DB)
    try:
        await http.connect()
        await http.signin("root", "root")
        await http.query(f"DEFINE NAMESPACE IF NOT EXISTS {NS}; DEFINE DATABASE IF NOT EXISTS {DB};")
        await http.query(f"REMOVE TABLE IF EXISTS {BIG_TABLE};")
        for i in range(6):
            await http.query(f"CREATE {BIG_TABLE}:{i} SET blob = string::repeat('x', 1000000);")
        yield
        await http.query(f"REMOVE TABLE IF EXISTS {BIG_TABLE};")
    finally:
        await http.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_reply_above_4_mib_is_received(big_rows: None) -> None:
    conn = WebSocketConnection("ws://localhost:8000", NS, DB, timeout=10, auto_reconnect=False)
    try:
        await conn.connect()
        await conn.signin("root", "root")
        await conn.use(NS, DB)
        started = time.monotonic()
        response = await conn.query(f"SELECT * FROM {BIG_TABLE} LIMIT 1000")
        elapsed = time.monotonic() - started

        rows = response.first_result.result
        assert len(rows) == 6
        assert all(len(row["blob"]) == 1_000_000 for row in rows)
        assert elapsed < 5
    finally:
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_reply_above_max_message_size_fails_at_once_then_reconnects(big_rows: None) -> None:
    conn = WebSocketConnection("ws://localhost:8000", NS, DB, timeout=30, reconnect_interval=0.05, max_message_size=4 * MiB)
    try:
        await conn.connect()
        await conn.signin("root", "root")
        await conn.use(NS, DB)
        started = time.monotonic()
        with pytest.raises(ConnectionError) as excinfo:
            await conn.query(f"SELECT * FROM {BIG_TABLE} LIMIT 1000")
        elapsed = time.monotonic() - started

        assert elapsed < 5
        assert str(excinfo.value).startswith("WebSocket connection lost: ")
        assert str(4 * MiB) in str(excinfo.value)

        # The read loop has started the reconnect; the next query is served by the new socket.
        deadline = time.monotonic() + 10
        while True:
            try:
                response = await conn.query(f"SELECT count() FROM {BIG_TABLE} GROUP ALL")
                break
            except ConnectionError:
                assert time.monotonic() < deadline, "the connection did not come back"
                await asyncio.sleep(0.05)
        assert response.first_result.result == [{"count": 6}]
    finally:
        await conn.close()
