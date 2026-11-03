"""Issue #453 — a WebSocket reply above 4 MiB arrives from the 2.x server.

aiohttp caps a received message at 4 MiB unless ``ws_connect()`` says
otherwise; a larger reply dropped the socket and the request waited for its
whole timeout. With an explicit ``max_message_size`` the request fails at once,
and the connection serves the next query after its reconnect.
"""

import asyncio
import time
from collections.abc import AsyncGenerator

import pytest

from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.exceptions import ConnectionError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

NS = SURREALDB_NAMESPACE
DB = "i453_ws_size"
TABLE = "i453_big"
MiB = 1024 * 1024
WS_URL = SURREALDB_URL.replace("http://", "ws://", 1).replace("https://", "wss://", 1)


@pytest.fixture(scope="module")
async def big_rows() -> AsyncGenerator[None, None]:
    """Six rows of 1 MB each, written over HTTP one statement at a time."""
    http = HTTPConnection(SURREALDB_URL, NS, DB)
    try:
        await http.connect()
        await http.signin(SURREALDB_USER, SURREALDB_PASS)
        await http.query(f"DEFINE NAMESPACE IF NOT EXISTS {NS}; DEFINE DATABASE IF NOT EXISTS {DB};")
        await http.query(f"REMOVE TABLE IF EXISTS {TABLE};")
        for i in range(6):
            await http.query(f"CREATE {TABLE}:{i} SET blob = string::repeat('x', 1000000);")
        yield
        await http.query(f"REMOVE TABLE IF EXISTS {TABLE};")
    finally:
        await http.close()


async def _ws(**kwargs: object) -> WebSocketConnection:
    conn = WebSocketConnection(WS_URL, NS, DB, **kwargs)  # type: ignore[arg-type]
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    await conn.use(NS, DB)
    return conn


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ws_reply_above_4_mib_is_received(big_rows: None) -> None:
    conn = await _ws(timeout=10, auto_reconnect=False)
    try:
        started = time.monotonic()
        response = await conn.query(f"SELECT * FROM {TABLE}")
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
    conn = await _ws(timeout=30, reconnect_interval=0.05, max_message_size=4 * MiB)
    try:
        started = time.monotonic()
        with pytest.raises(ConnectionError) as excinfo:
            await conn.query(f"SELECT * FROM {TABLE}")
        elapsed = time.monotonic() - started

        assert elapsed < 5
        assert str(excinfo.value).startswith("WebSocket connection lost: ")
        assert str(4 * MiB) in str(excinfo.value)

        # The read loop has started the reconnect; the next query is served by the new socket.
        deadline = time.monotonic() + 10
        while True:
            try:
                response = await conn.query(f"SELECT count() FROM {TABLE} GROUP ALL")
                break
            except ConnectionError:
                assert time.monotonic() < deadline, "the connection did not come back"
                await asyncio.sleep(0.05)
        assert response.first_result.result == [{"count": 6}]
    finally:
        await conn.close()
