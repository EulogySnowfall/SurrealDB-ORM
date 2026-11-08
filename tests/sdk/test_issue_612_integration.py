"""#612 against the pinned server: a ``date`` inside a record id key.

A plain ``date`` has been sent as a UTC-midnight datetime (tag 12) since 0.38.5,
but inside a ``RecordId`` it went out as cbor2's native tag 1004, which
SurrealDB does not read: HTTP answered ``400 Parse error`` for the whole
request and WebSocket never replied. The record id walk now converts it, so a
record created with such a key is found again by the same ``RecordId``.
"""

from collections.abc import AsyncGenerator
from datetime import date

import pytest

from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.protocol.cbor import RecordId

NS = "test"
DB = "i612_cbor_record_id"
TABLE = "i612_day"


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
        await connection.query(f"REMOVE TABLE IF EXISTS {TABLE};")
        yield connection
        await connection.query(f"REMOVE TABLE IF EXISTS {TABLE};")
    finally:
        await connection.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_date_in_record_id_key_round_trips(conn: BaseSurrealConnection) -> None:
    record_id = RecordId(TABLE, ["a", date(2026, 1, 2)])

    await conn.query("CREATE $rid SET n = 1;", {"rid": record_id})
    found = await conn.query("SELECT VALUE n FROM $rid;", {"rid": record_id})
    assert found.first_result.result == [1]

    stored = await conn.query(f"SELECT VALUE <string> id FROM {TABLE};")
    assert stored.first_result.result == [f"{TABLE}:['a', d'2026-01-02T00:00:00Z']"]
