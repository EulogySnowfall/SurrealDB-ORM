"""Integration tests for #277: a live stream keeps receiving events after auto-resubscribe (v2 line).

Backport of #276. ``LiveSelectStream`` (what every ``QuerySet.live()`` uses)
registered its subscription for resubscription without a callback, and nothing
moved it to the re-created live query. After the socket dropped, the connection
re-created the ``LIVE SELECT`` with the same inlined filter, but the stream's
handler was gone and it still held the old id: no further event reached the
iterator, and ``stop()`` killed a live query that no longer existed.

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

SURREALDB_DATABASE = "test_issue_277_v2"

#: Bounded waits, never sleeps: the reconnect itself takes well under a second.
RECONNECT_TIMEOUT = 15.0
EVENT_TIMEOUT = 5.0


class I277Item(BaseSurrealModel):
    """A record the live filter selects on ``tag``."""

    model_config = SurrealConfigDict(table_name="i277_items")

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
    await client.query("REMOVE TABLE IF EXISTS i277_items; DEFINE TABLE i277_items SCHEMALESS;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i277_items;")


async def _write(sql: str) -> None:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    assert response.is_ok, response.raw


async def _server_live_ids() -> set[str]:
    """The live queries the server holds on the table, by id."""
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query("INFO FOR TABLE i277_items;")
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
            conn, "i277_items", where="tag = $t", params={"t": "wanted"}, auto_resubscribe=True, on_reconnect=reconnects
        )
        await stream.start()
        first_id = stream.live_id

        await _write("CREATE i277_items:before SET tag = 'wanted'")
        before = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)
        assert before.record_id == "i277_items:before"

        await _drop_socket(conn)
        old_id, new_id = await reconnects.wait()
        assert old_id == first_id and new_id != first_id
        assert stream.live_id == new_id
        assert new_id in await _server_live_ids()

        await _write("CREATE i277_items:decoy SET tag = 'other'")
        await _write("CREATE i277_items:after SET tag = 'wanted'")
        after = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)
        assert after.record_id == "i277_items:after"

        await stream.stop()
        assert new_id not in conn._live_callbacks
        assert new_id not in conn._live_subscriptions
        assert new_id not in await _server_live_ids()
    finally:
        await conn.close()


@pytest.mark.integration
async def test_queryset_live_receives_events_after_reconnect() -> None:
    reconnects = _Reconnects()
    stream = I277Item.objects().filter(tag="wanted").live(on_reconnect=reconnects)
    async with stream:
        conn = stream._connection
        assert conn is not None
        conn.reconnect_interval = 0.05
        first_id = stream.live_id

        await _write("CREATE i277_items:before SET tag = 'wanted'")
        before = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)
        assert before.instance.id == "before"

        await _drop_socket(conn)
        old_id, new_id = await reconnects.wait()
        assert old_id == first_id and new_id != first_id
        assert stream.live_id == new_id

        await _write("CREATE i277_items:decoy SET tag = 'other'")
        await _write("CREATE i277_items:after SET tag = 'wanted'")
        after = await asyncio.wait_for(anext(stream), timeout=EVENT_TIMEOUT)
        assert after.instance.id == "after"
        assert after.instance.tag == "wanted"

    assert new_id not in conn._live_callbacks
    assert new_id not in conn._live_subscriptions
    assert new_id not in await _server_live_ids()


@pytest.mark.integration
async def test_stop_during_the_resubscribe_leaves_no_live_query_behind() -> None:
    """stop() issued while the re-created LIVE SELECT is in flight ends that query too.

    The stream must count as stopped from the start of ``stop()``: its kill
    targets the old id, which ended with the socket, so if the stream adopted
    the re-created id meanwhile, that query kept running on the server.
    """
    conn = WebSocketConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, reconnect_interval=0.05)
    await conn.connect()
    try:
        await conn.signin(SURREALDB_USER, SURREALDB_PASS)
        reconnects = _Reconnects()
        stream = LiveSelectStream(
            conn, "i277_items", where="tag = $t", params={"t": "wanted"}, auto_resubscribe=True, on_reconnect=reconnects
        )
        await stream.start()

        original_query = conn.query
        original_resubscribe_all = conn._resubscribe_all
        stoppers: list[asyncio.Task[None]] = []
        recreated: list[str] = []
        resubscribed = asyncio.Event()

        async def query(sql: str, vars: dict[str, Any] | None = None) -> Any:
            if sql.startswith("LIVE SELECT") and not stoppers:
                # Another task stops the stream while this LIVE SELECT is in flight.
                stoppers.append(asyncio.create_task(stream.stop()))
                response = await original_query(sql, vars)
                recreated.append(str(response.results[0].result))
                return response
            return await original_query(sql, vars)

        async def resubscribe_all() -> None:
            await original_resubscribe_all()
            resubscribed.set()

        conn.query = query  # type: ignore[method-assign]
        conn._resubscribe_all = resubscribe_all  # type: ignore[method-assign]
        await _drop_socket(conn)
        await asyncio.wait_for(resubscribed.wait(), timeout=RECONNECT_TIMEOUT)
        await asyncio.wait_for(stoppers[0], timeout=EVENT_TIMEOUT)
        conn.query = original_query  # type: ignore[method-assign]

        assert len(recreated) == 1
        assert stream.live_id is None
        assert conn._live_subscriptions == {}
        assert conn._live_callbacks == {}
        assert recreated[0] not in await _server_live_ids()
        assert reconnects.calls == []
    finally:
        await conn.close()
