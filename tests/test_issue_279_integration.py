"""Integration tests for #279: the callback live-query APIs survive a reconnect.

``WebSocketConnection.live(table, callback)`` and ``LiveQuery.subscribe()``
registered only a handler, no subscription to re-create, so after the socket
dropped the connection reconnected but never re-created their ``LIVE SELECT``:
the callback received nothing more, with no error.

Each test receives a matching ``CREATE``, closes the socket, waits until the
connection has re-created the live query (a new id), and expects the next
``CREATE`` in the callback. The server's own list of live queries
(``INFO FOR TABLE``) shows that ``kill()`` / ``unsubscribe()`` end the
re-created one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest

from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.streaming.live_query import LiveNotification, LiveQuery
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_279"
TABLE = "i279_items"

#: Bounded waits: the reconnect itself takes well under a second.
RECONNECT_TIMEOUT = 15.0
EVENT_TIMEOUT = 5.0


@pytest.fixture
async def conn() -> AsyncGenerator[WebSocketConnection, Any]:
    """A signed-in WebSocket connection on a fresh table, reconnecting quickly."""
    connection = WebSocketConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, reconnect_interval=0.05)
    await connection.connect()
    await connection.signin(SURREALDB_USER, SURREALDB_PASS)
    await connection.query(f"DEFINE DATABASE IF NOT EXISTS {SURREALDB_DATABASE};")
    await connection.use(SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    response = await connection.query(f"REMOVE TABLE IF EXISTS {TABLE}; DEFINE TABLE {TABLE} SCHEMALESS;")
    assert response.is_ok, response.raw
    try:
        yield connection
    finally:
        await connection.query(f"REMOVE TABLE IF EXISTS {TABLE};")
        await connection.close()


async def _write(conn: WebSocketConnection, record: str) -> None:
    response = await conn.query(f"CREATE {TABLE}:{record} SET tag = 'x'")
    assert response.is_ok, response.raw


async def _server_live_ids(conn: WebSocketConnection) -> set[str]:
    """The live queries the server holds on the table, by id."""
    response = await conn.query(f"INFO FOR TABLE {TABLE};")
    assert response.is_ok, response.raw
    info = response.results[0].result
    return {str(live_id) for live_id in (info.get("lives") or {})}


async def _until(predicate: Callable[[], bool], timeout: float) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


class _Events:
    """A callback that records each notification and signals its arrival."""

    def __init__(self) -> None:
        self.received: list[Any] = []
        self._arrived = asyncio.Event()

    async def __call__(self, item: Any) -> None:
        self.received.append(item)
        self._arrived.set()

    async def next(self) -> Any:
        await asyncio.wait_for(self._arrived.wait(), timeout=EVENT_TIMEOUT)
        self._arrived.clear()
        return self.received[-1]


async def _drop_socket_and_wait(conn: WebSocketConnection, recreated: Callable[[], bool]) -> None:
    """Close the socket under the connection, as a server restart would, and wait for the re-created query."""
    assert conn._ws is not None
    await conn._ws.close()
    await _until(lambda: recreated() and conn._session_ready.is_set(), RECONNECT_TIMEOUT)


@pytest.mark.integration
async def test_callback_live_receives_events_after_reconnect(conn: WebSocketConnection) -> None:
    events = _Events()
    first_id = await conn.live(TABLE, events)

    await _write(conn, "before")
    before = await events.next()
    assert str(before["result"]["id"]) == f"{TABLE}:before"

    await _drop_socket_and_wait(conn, lambda: bool(conn.live_queries) and conn.live_queries != [first_id])
    (new_id,) = conn.live_queries
    assert new_id in await _server_live_ids(conn)

    await _write(conn, "after")
    after = await events.next()
    assert str(after["result"]["id"]) == f"{TABLE}:after"
    assert str(after["id"]) == new_id

    # The id live() returned still ends the re-created query.
    await conn.kill(first_id)
    assert conn.live_queries == []
    assert conn._live_subscriptions == {}
    assert new_id not in await _server_live_ids(conn)


@pytest.mark.integration
async def test_live_query_receives_events_after_reconnect(conn: WebSocketConnection) -> None:
    events = _Events()
    live = LiveQuery(conn, TABLE, where="tag = 'x'")
    first_id = await live.subscribe(events)

    await _write(conn, "before")
    before = await events.next()
    assert isinstance(before, LiveNotification)
    assert str(before.result["id"]) == f"{TABLE}:before"

    await _drop_socket_and_wait(conn, lambda: live.live_id is not None and live.live_id != first_id)
    new_id = live.live_id
    assert new_id is not None
    assert new_id in await _server_live_ids(conn)

    await _write(conn, "after")
    after = await events.next()
    assert str(after.result["id"]) == f"{TABLE}:after"
    assert str(after.id) == new_id

    await live.unsubscribe()
    assert conn._live_subscriptions == {}
    assert conn._live_callbacks == {}
    assert new_id not in await _server_live_ids(conn)
