"""Integration tests for #578: datetimes, dates and durations sent as CBOR tags 12/14.

The SDK used to send a ``datetime`` (and a ``date``, turned into a UTC-midnight
``datetime``) as the standard tag 0, an ISO string, and a text ``Duration`` as
tag 13. SurrealDB 4.0 refuses both; tag 12 and tag 14 ``[seconds,
nanoseconds]`` are read by every server since 1.5. These tests check that the
server result is the one 3.x gave before: bound values over HTTP and WebSocket,
``save()``, ``filter()``, ``raw_query()`` and ``live()`` filters.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.protocol.cbor import Duration
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_578"

PLUS_TWO = timezone(timedelta(hours=2))
AT = datetime(2026, 2, 1, 12, 30, 15, 123456, tzinfo=UTC)
#: ``AT`` as SurrealQL writes it.
AT_LITERAL = "d'2026-02-01T12:30:15.123456Z'"
BEFORE_1970 = datetime(1969, 7, 20, 20, 17, 40, 500000, tzinfo=UTC)
BEFORE_1970_LITERAL = "d'1969-07-20T20:17:40.500Z'"
DAY = date(2026, 2, 1)
MIDNIGHT = datetime(2026, 2, 1, tzinfo=UTC)

EVENT_TIMEOUT = 5.0

UUID_TEXT = "12345678-1234-5678-1234-567812345678"


class I578MyUUID(uuid.UUID):
    """A ``UUID`` subclass, as ``asyncpg.pgproto.pgproto.UUID`` is."""


class I578Event(BaseSurrealModel):
    """A schemaless record holding a datetime, a date and a duration.

    ``save()`` does not carry a ``Duration`` model value (Pydantic dumps the
    dataclass as an object, before and after #578), so durations are written
    with bound queries here and only read back through the model.
    """

    model_config = SurrealConfigDict(table_name="i578_events")

    id: str | None = None
    at: datetime | None = None
    day: date | None = None
    ttl: Any = None


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, Any]:
    """Point the ORM at a database of its own for this module."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def clean_tables() -> AsyncGenerator[None, Any]:
    """A fresh table: a LIVE SELECT needs the table to exist."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i578_events; DEFINE TABLE i578_events SCHEMALESS;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i578_events;")


async def _sdk_connection(kind: str) -> BaseSurrealConnection:
    conn: BaseSurrealConnection
    if kind == "http":
        conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    else:
        conn = WebSocketConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    return conn


async def _first(conn: BaseSurrealConnection, sql: str, params: dict[str, Any]) -> Any:
    response = await asyncio.wait_for(conn.query(sql, params), timeout=EVENT_TIMEOUT)
    assert response.is_ok, response.raw
    return response.results[0].result


# ==================== Bound values ====================


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_bound_datetimes_are_the_same_instants(kind: str) -> None:
    conn = await _sdk_connection(kind)
    try:
        result = await _first(
            conn,
            f"RETURN [$at = {AT_LITERAL}, $old = {BEFORE_1970_LITERAL}, $tz = {AT_LITERAL}, $at, $old, $tz]",
            {"at": AT, "old": BEFORE_1970, "tz": AT.astimezone(PLUS_TWO)},
        )
        assert result == [True, True, True, AT, BEFORE_1970, AT]
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_bound_date_is_utc_midnight(kind: str) -> None:
    conn = await _sdk_connection(kind)
    try:
        result = await _first(conn, "RETURN [type::of($d), $d = d'2026-02-01', $d]", {"d": DAY})
        assert result == ["datetime", True, MIDNIGHT]
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_bound_text_duration_is_the_same_duration(kind: str) -> None:
    conn = await _sdk_connection(kind)
    try:
        result = await _first(
            conn,
            "RETURN [type::of($d), $d = 1h30m, $n = 1y2w3d4h5m6s7ms8us9ns, $z = 0ns, $d, $n]",
            {"d": Duration("1h30m"), "n": Duration("1y2w3d4h5m6s7ms8us9ns"), "z": Duration("0ns")},
        )
        assert result == ["duration", True, True, True, Duration((5400,)), Duration((33019506, 7008009))]
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_bound_values_inside_a_record_id_key(kind: str) -> None:
    conn = await _sdk_connection(kind)
    try:
        await _first(conn, "CREATE type::record('i578_events', [$at, $d]) SET n = 1", {"at": AT, "d": Duration("2h")})
        found = await _first(conn, f"SELECT VALUE n FROM i578_events:[{AT_LITERAL}, 2h]", {})
        assert found == [1]
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_bound_uuid_subclass_is_a_uuid(kind: str) -> None:
    conn = await _sdk_connection(kind)
    try:
        result = await _first(
            conn,
            f"RETURN [type::of($u), $u = u'{UUID_TEXT}', $u]",
            {"u": I578MyUUID(UUID_TEXT)},
        )
        assert result == ["uuid", True, uuid.UUID(UUID_TEXT)]
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_invalid_duration_and_bignum_fail_before_any_request(kind: str) -> None:
    """Both raise ``ValueError`` at once; the connection keeps working."""
    conn = await _sdk_connection(kind)
    try:
        with pytest.raises(ValueError, match="duration"):
            await asyncio.wait_for(conn.query("RETURN $d", {"d": Duration("1h 30m")}), timeout=EVENT_TIMEOUT)
        with pytest.raises(ValueError, match="64-bit"):
            await asyncio.wait_for(conn.query("RETURN $n", {"n": 2**64 + 5}), timeout=EVENT_TIMEOUT)
        assert await _first(conn, "RETURN $n", {"n": 1}) == 1
    finally:
        await conn.close()


# ==================== ORM paths ====================


@pytest.mark.integration
async def test_save_stores_the_values_the_server_compares_equal() -> None:
    await I578Event(id="one", at=AT, day=DAY).save()

    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(f"SELECT VALUE [at = {AT_LITERAL}, day = d'2026-02-01'] FROM i578_events:one")
    assert response.is_ok, response.raw
    assert response.results[0].result == [[True, True]]

    loaded = await I578Event.objects().get("one")
    assert (loaded.at, loaded.day) == (AT, DAY)


@pytest.mark.integration
async def test_filter_matches_datetime_date_and_duration() -> None:
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        f"CREATE i578_events:match SET at = {AT_LITERAL}, day = d'2026-02-01', ttl = 90m;"
        "CREATE i578_events:decoy SET at = d'2025-01-01T08:00:00Z', day = d'2025-01-01', ttl = 1h;"
        f"CREATE i578_events:old SET at = {BEFORE_1970_LITERAL};"
    )

    assert [e.id for e in await I578Event.objects().filter(at=AT).exec()] == ["match"]
    assert [e.id for e in await I578Event.objects().filter(day=DAY).exec()] == ["match"]
    assert [e.id for e in await I578Event.objects().filter(ttl=Duration("1h30m")).exec()] == ["match"]
    assert [e.id for e in await I578Event.objects().filter(at__lt=datetime(1970, 1, 1, tzinfo=UTC)).exec()] == ["old"]


@pytest.mark.integration
async def test_raw_query_binds_datetime_date_and_duration() -> None:
    await I578Event.raw_query(
        "CREATE i578_events:raw SET at = $at, day = $day, ttl = $ttl",
        variables={"at": AT, "day": DAY, "ttl": Duration("2h")},
    )

    rows = await I578Event.raw_query(
        "SELECT VALUE record::id(id) FROM i578_events WHERE at = $at AND day = $day AND ttl = $ttl",
        variables={"at": AT, "day": DAY, "ttl": Duration("120m")},
    )
    assert rows == ["raw"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("field", "value", "literal"),
    [
        ("at", AT, AT_LITERAL),
        ("day", DAY, "d'2026-02-01'"),
        ("ttl", Duration("1h30m"), "90m"),
    ],
)
async def test_live_filter_receives_the_matching_create(field: str, value: Any, literal: str) -> None:
    client = await SurrealDBConnectionManager.get_client()
    async with I578Event.objects().filter(**{field: value}).live() as stream:
        await client.query(f"CREATE i578_events:decoy SET {field} = d'2020-01-01T00:00:00Z'")
        await client.query(f"CREATE i578_events:match SET {field} = {literal}")
        event = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)

    assert event.record_id == "i578_events:match"
