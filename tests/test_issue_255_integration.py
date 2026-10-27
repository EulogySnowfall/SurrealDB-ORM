"""Integration tests for #255: a live stream keeps receiving events after auto-resubscribe.

``LiveSelectStream`` — what every ``QuerySet.live()`` uses — registered its
subscription for resubscription without a callback, and nothing moved it to
the re-created live query. After the socket dropped, the connection re-created
the ``LIVE SELECT`` with the right bound filter, but the stream's handler was
gone and it still held the old id: no further event reached the iterator, and
``stop()`` killed a live query that no longer existed.

Each test receives a matching ``CREATE``, closes the socket, waits for
``on_reconnect`` and then expects the next matching ``CREATE`` (after a decoy
that must not match) from the iterator. The server's own list of live queries
(``INFO FOR TABLE``) shows that ``stop()`` ends the re-created one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.streaming.live_select import LiveSelectStream
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_255"

#: Bounded waits, never sleeps: the reconnect itself takes well under a second.
RECONNECT_TIMEOUT = 15.0
EVENT_TIMEOUT = 5.0


class I255Item(BaseSurrealModel):
    """A record the live filter selects on ``tag``."""

    model_config = SurrealConfigDict(table_name="i255_items")

    id: str | None = None
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
    await client.query("REMOVE TABLE IF EXISTS i255_items; DEFINE TABLE i255_items SCHEMALESS;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i255_items;")


async def _write(sql: str) -> None:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    assert response.is_ok, response.raw


async def _server_live_ids() -> set[str]:
    """The live queries the server holds on the table, by id."""
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query("INFO FOR TABLE i255_items;")
    assert response.is_ok, response.raw
    info = response.results[0].result
    return {str(live_id) for live_id in (info.get("lives") or {})}


class _Reconnects:
    """A user ``on_reconnect`` that records ``(old_id, new_id)`` and signals."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.event = asyncio.Event()

    async def __call__(self, old_id: str, new_id: str) -> None:
        self.calls.append((old_id, new_id))
        self.event.set()

    async def wait(self) -> tuple[str, str]:
        await asyncio.wait_for(self.event.wait(), timeout=RECONNECT_TIMEOUT)
        return self.calls[0]


async def _drop_socket(conn: WebSocketConnection) -> None:
    """Close the socket under the connection, as a server restart would."""
    assert conn._ws is not None
    await conn._ws.close()


@pytest.mark.integration
async def test_live_select_stream_receives_events_after_reconnect() -> None:
    conn = WebSocketConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, reconnect_interval=0.05)
    await conn.connect()
    try:
        await conn.signin(SURREALDB_USER, SURREALDB_PASS)
        reconnects = _Reconnects()
        stream = LiveSelectStream(
            conn, "i255_items", where="tag = $t", params={"t": "wanted"}, auto_resubscribe=True, on_reconnect=reconnects
        )
        await stream.start()
        first_id = stream.live_id

        await _write("CREATE i255_items:before SET tag = 'wanted'")
        before = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)
        assert before.record_id == "i255_items:before"

        await _drop_socket(conn)
        old_id, new_id = await reconnects.wait()
        assert old_id == first_id and new_id != first_id
        assert stream.live_id == new_id
        assert new_id in await _server_live_ids()

        await _write("CREATE i255_items:decoy SET tag = 'other'")
        await _write("CREATE i255_items:after SET tag = 'wanted'")
        after = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)
        assert after.record_id == "i255_items:after"

        await stream.stop()
        assert new_id not in conn._live_callbacks
        assert new_id not in conn._live_subscriptions
        assert new_id not in await _server_live_ids()
    finally:
        await conn.close()


@pytest.mark.integration
async def test_queryset_live_receives_events_after_reconnect() -> None:
    reconnects = _Reconnects()
    stream = I255Item.objects().filter(tag="wanted").live(on_reconnect=reconnects)
    async with stream:
        conn = stream._connection
        assert conn is not None
        conn.reconnect_interval = 0.05
        first_id = stream.live_id

        await _write("CREATE i255_items:before SET tag = 'wanted'")
        before = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)
        assert before.instance.id == "before"

        await _drop_socket(conn)
        old_id, new_id = await reconnects.wait()
        assert old_id == first_id and new_id != first_id
        assert stream.live_id == new_id

        await _write("CREATE i255_items:decoy SET tag = 'other'")
        await _write("CREATE i255_items:after SET tag = 'wanted'")
        after = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)
        assert after.instance.id == "after"
        assert after.instance.tag == "wanted"

    assert new_id not in conn._live_callbacks
    assert new_id not in conn._live_subscriptions
    assert new_id not in await _server_live_ids()
