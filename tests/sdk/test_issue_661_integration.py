"""#661 against the pinned server: encoding a NamedTuple value.

Binding a NamedTuple (as a query variable, in a record, or nested) raised
TypeError in _preprocess_for_cbor() before anything was sent. A NamedTuple is
now encoded like a tuple (a CBOR array of its converted items).
"""

from collections.abc import AsyncGenerator
from typing import NamedTuple

import pytest

from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection

NS = "test"
DB = "i661_cbor_namedtuple"
TABLE = "i661_item"


class Point(NamedTuple):
    x: int
    y: int


@pytest.fixture(params=["http", "ws"])
async def conn(request: pytest.FixtureRequest) -> AsyncGenerator[BaseSurrealConnection, None]:
    """A signed-in CBOR connection on each transport."""
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
async def test_namedtuple_round_trips(conn: BaseSurrealConnection) -> None:
    point = Point(x=10, y=20)

    # Bind as query variable
    res = await conn.query("RETURN $pt;", {"pt": point})
    assert res.first_result.result == [10, 20]

    # Nested in record data
    await conn.query(f"CREATE {TABLE}:first CONTENT $data;", {"data": {"coord": point, "list": [point]}})
    found = await conn.query(f"SELECT coord, list FROM {TABLE}:first;")
    assert found.first_result.result == [{"coord": [10, 20], "list": [[10, 20]]}]
