"""Issue #452 — a ``Duration`` bound as a query parameter reaches the 2.x server as a duration.

A string ``Duration`` used to be sent as CBOR tag 14 (the compact array form),
which the server cannot read: HTTP answered ``400`` for the whole request and
WebSocket never replied. Covered over both transports, plus the round trip of a
duration the server sends (compact form) back into a query.
"""

from collections.abc import AsyncGenerator

import pytest

from src.surreal_sdk.connection.base import BaseSurrealConnection
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.protocol.cbor import Duration
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

NS = SURREALDB_NAMESPACE
DB = "i452_duration"


@pytest.fixture(params=["http", "ws"])
async def conn(request: pytest.FixtureRequest) -> AsyncGenerator[BaseSurrealConnection, None]:
    """A signed-in CBOR connection on each transport (WebSocket with a short timeout, so a hang fails fast)."""
    connection: BaseSurrealConnection
    if request.param == "http":
        connection = HTTPConnection(SURREALDB_URL, NS, DB)
    else:
        ws_url = SURREALDB_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        connection = WebSocketConnection(ws_url, NS, DB, timeout=5, auto_reconnect=False)
    try:
        await connection.connect()
        await connection.signin(SURREALDB_USER, SURREALDB_PASS)
        await connection.query(f"DEFINE NAMESPACE IF NOT EXISTS {NS}; DEFINE DATABASE IF NOT EXISTS {DB};")
        yield connection
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_string_duration_binds_as_duration(conn: BaseSurrealConnection) -> None:
    response = await conn.query("RETURN [type::is::duration($d), <string>$d]", {"d": Duration("1h30m")})
    assert response.first_result is not None
    assert response.first_result.result == [True, "1h30m"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_compact_duration_binds_as_duration(conn: BaseSurrealConnection) -> None:
    response = await conn.query("RETURN [type::is::duration($d), <string>$d]", {"d": Duration([5400, 1])})
    assert response.first_result is not None
    assert response.first_result.result == [True, "1h30m1ns"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_duration_read_back_rebinds_equal(conn: BaseSurrealConnection) -> None:
    read = await conn.query("RETURN 1h30m1ns")
    assert read.first_result is not None
    value = read.first_result.result
    assert isinstance(value, Duration)
    assert str(value) == "5400s1ns"

    response = await conn.query("RETURN [$d = 1h30m1ns, $s = 1h30m1ns]", {"d": value, "s": Duration(str(value))})
    assert response.first_result is not None
    assert response.first_result.result == [True, True]
