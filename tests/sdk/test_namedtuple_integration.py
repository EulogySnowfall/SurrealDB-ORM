"""Named tuples travel as CBOR arrays through both SDK transports."""

from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import NamedTuple

import pytest

from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from tests.conftest import SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER, SURREALDB_WS_URL


@pytest.fixture(params=["http", "ws"])
async def conn(request: pytest.FixtureRequest) -> AsyncGenerator[BaseSurrealConnection, None]:
    connection: BaseSurrealConnection
    if request.param == "http":
        connection = HTTPConnection(SURREALDB_URL, "test", "namedtuple_roundtrip")
    else:
        connection = WebSocketConnection(SURREALDB_WS_URL, "test", "namedtuple_roundtrip", timeout=5, auto_reconnect=False)
    try:
        await connection.connect()
        await connection.signin(SURREALDB_USER, SURREALDB_PASS)
        await connection.query("DEFINE NAMESPACE IF NOT EXISTS test; DEFINE DATABASE IF NOT EXISTS namedtuple_roundtrip;")
        yield connection
    finally:
        await connection.close()


@pytest.mark.integration
async def test_namedtuple_query_variable_round_trip(conn: BaseSurrealConnection) -> None:
    class Point(NamedTuple):
        amount: Decimal
        day: date

    point = Point(Decimal("1.25"), date(2026, 1, 2))
    result = await conn.query("RETURN $value;", {"value": {"point": point, "points": [point]}})
    expected = [Decimal("1.25"), datetime(2026, 1, 2, tzinfo=UTC)]
    assert result.first_result.result == {"point": expected, "points": [expected]}
