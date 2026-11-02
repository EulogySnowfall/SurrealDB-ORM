"""SDK fixes carried by v0.37.0, against the pinned server.

I13: a ``Duration`` bound as a query parameter reaches the server as a duration,
over HTTP and over WebSocket. A string ``Duration`` used to be sent as CBOR tag 14
(the compact array form), which the server cannot read: HTTP answered
``400 Parse error`` for the whole request and WebSocket never replied.
"""

from collections.abc import AsyncGenerator

import pytest

from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.protocol.cbor import Duration

NS = "test"
DB = "dwf_duration_ws"


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
