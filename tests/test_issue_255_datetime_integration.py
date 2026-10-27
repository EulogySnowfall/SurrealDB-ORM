"""Integration tests for #255: a naive datetime reaches SurrealDB as its UTC instant.

Over CBOR, the default protocol, a ``datetime`` with no ``tzinfo`` used to be
refused by the encoder before anything was sent (``CBOREncodeError``, a
``ConnectionError`` over WebSocket): a bound ``$d``, ``save()``/``merge()`` of a
model holding one, and ``filter()``/``live()`` on one all failed. It is now
read as UTC, as ``raw_query(inline_dicts=True)`` already did, with a
``RuntimeWarning``.

A ``datetime`` subclass was sent as tag 12 carrying an ISO string, which the
server rejects as a parse error (HTTP 400), aware or not. It now travels like a
plain ``datetime``.
"""

from __future__ import annotations

import asyncio
import warnings
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_255_dt"

NAIVE = datetime(2026, 2, 1, 12, 30, 15, 123456)
AS_UTC = NAIVE.replace(tzinfo=UTC)
#: The same instant as ``AS_UTC``, as SurrealQL writes it.
LITERAL = "d'2026-02-01T12:30:15.123456Z'"
OTHER = datetime(2025, 1, 1, 8, 0)
PLUS_TWO = timezone(timedelta(hours=2))

EVENT_TIMEOUT = 5.0


class I255MyDT(datetime):
    """A ``datetime`` subclass, as ``pandas.Timestamp`` or ``pendulum.DateTime`` are."""


class I255Event(BaseSurrealModel):
    """A record holding one datetime."""

    model_config = SurrealConfigDict(table_name="i255_events")

    id: str | None = None
    at: datetime | None = None
    tag: str = Field(default="")


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
    await client.query("REMOVE TABLE IF EXISTS i255_events; DEFINE TABLE i255_events SCHEMALESS;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i255_events;")


@contextmanager
def _warns_naive() -> Iterator[None]:
    with pytest.warns(RuntimeWarning, match="read as UTC"):
        yield


async def _sdk_connection(kind: str) -> BaseSurrealConnection:
    conn: BaseSurrealConnection
    if kind == "http":
        conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    else:
        conn = WebSocketConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    return conn


async def _return(conn: BaseSurrealConnection, value: Any) -> Any:
    response = await conn.query("RETURN [type::of($d), $d]", {"d": value})
    assert response.is_ok, response.raw
    return response.results[0].result


async def _ids_at(literal: str) -> list[str]:
    """The records whose ``at`` equals *literal*, compared on the server."""
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(f"SELECT VALUE record::id(id) FROM i255_events WHERE at = {literal}")
    assert response.is_ok, response.raw
    return list(response.results[0].result)


# ==================== Bound values ====================


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_bound_naive_datetime_returns_the_utc_instant(kind: str) -> None:
    conn = await _sdk_connection(kind)
    try:
        with _warns_naive():
            result = await _return(conn, NAIVE)
        assert result == ["datetime", AS_UTC]

        with _warns_naive():
            nested = await _return(conn, {"list": [NAIVE], "obj": {"at": NAIVE}})
        assert nested == ["object", {"list": [AS_UTC], "obj": {"at": AS_UTC}}]
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_bound_aware_datetime_is_unchanged(kind: str) -> None:
    conn = await _sdk_connection(kind)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            result = await _return(conn, datetime(2026, 2, 1, 14, 30, tzinfo=PLUS_TWO))
        assert result == ["datetime", datetime(2026, 2, 1, 12, 30, tzinfo=UTC)]
    finally:
        await conn.close()


@pytest.mark.integration
@pytest.mark.parametrize("kind", ["http", "ws"])
async def test_bound_datetime_subclass_is_accepted(kind: str) -> None:
    """A subclass used to be sent as tag 12 with an ISO string: a parse error, aware or not."""
    conn = await _sdk_connection(kind)
    try:
        aware = await _return(conn, I255MyDT(2026, 2, 1, 14, 30, tzinfo=PLUS_TWO))
        assert aware == ["datetime", datetime(2026, 2, 1, 12, 30, tzinfo=UTC)]

        with _warns_naive():
            naive = await _return(conn, I255MyDT(2026, 2, 1, 12, 30, 15, 123456))
        assert naive == ["datetime", AS_UTC]
    finally:
        await conn.close()


# ==================== Model writes and filters ====================


@pytest.mark.integration
async def test_save_of_a_naive_datetime_stores_the_utc_instant() -> None:
    event = I255Event(id="naive", at=NAIVE)
    with _warns_naive():
        await event.save()

    assert await _ids_at(LITERAL) == ["naive"]
    loaded = await I255Event.objects().get("naive")
    assert loaded.at == AS_UTC


@pytest.mark.integration
async def test_merge_of_a_naive_datetime_stores_the_utc_instant() -> None:
    event = I255Event(id="merged", at=datetime(2020, 1, 1, tzinfo=UTC))
    await event.save()

    with _warns_naive():
        await event.merge(at=NAIVE)

    assert await _ids_at(LITERAL) == ["merged"]


@pytest.mark.integration
async def test_filter_on_a_naive_datetime_matches_the_utc_instant() -> None:
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        f"CREATE i255_events:match SET at = {LITERAL}; CREATE i255_events:decoy SET at = d'2025-01-01T08:00:00Z';"
    )

    with _warns_naive():
        found = await I255Event.objects().filter(at=NAIVE).exec()

    assert [(item.id, item.at) for item in found] == [("match", AS_UTC)]


@pytest.mark.integration
async def test_live_filter_on_a_naive_datetime_receives_the_matching_create() -> None:
    client = await SurrealDBConnectionManager.get_client()
    with _warns_naive():
        async with I255Event.objects().filter(at=NAIVE).live() as stream:
            await client.query("CREATE i255_events:decoy SET at = $d", {"d": OTHER.replace(tzinfo=UTC)})
            await client.query("CREATE i255_events:match SET at = $d", {"d": AS_UTC})
            event = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)

    assert event.record_id == "i255_events:match"
    assert event.instance.at == AS_UTC
