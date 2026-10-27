"""Unit tests for #255: a live stream survives an auto-resubscribe.

``LiveSelectStream`` receives notifications through its own entry in the
connection's ``_live_callbacks`` and registered its subscription for
resubscription with ``callback=None``. After a reconnect,
``WebSocketConnection._resubscribe_all()`` cleared ``_live_callbacks`` and
re-created the live query, but nothing re-registered the stream's handler and
nothing moved the stream to the new id. The stream then received no further
event, and ``stop()`` killed the old id, so the new live query kept running
and was re-created again on every later reconnect.

The connection is a real ``WebSocketConnection`` that is never connected:
``query()`` and ``kill()`` are replaced at the method level, and each test
drives ``_resubscribe_all()`` directly, which is what the reconnect path runs
once the new socket is up.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.streaming.live_select import LiveSelectStream, LiveSubscriptionParams
from surreal_sdk.types import QueryResponse

WHERE = "tag = $t"
PARAMS = {"t": "x"}


class _FakeServer:
    """Hands out live ids in order and records every ``kill``."""

    def __init__(self, conn: WebSocketConnection, *ids: str) -> None:
        self._ids = list(ids)
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.killed: list[str] = []
        self.before_reply: Any = None
        # Held replies: the LIVE SELECT reply waits for ``query_gate``, and the
        # reply to ``kill(id)`` waits for ``kill_gates[id]``.
        self.query_gate: asyncio.Event | None = None
        self.kill_gates: dict[str, asyncio.Event] = {}
        conn.query = self.query  # type: ignore[method-assign]
        conn.kill = self.kill  # type: ignore[method-assign]
        self._conn = conn

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        self.queries.append((sql, vars))
        live_id = self._ids.pop(0)
        if self.before_reply is not None:
            hook, self.before_reply = self.before_reply, None
            await hook()
        if self.query_gate is not None:
            gate, self.query_gate = self.query_gate, None
            await gate.wait()
        return QueryResponse.from_rpc_result([{"status": "OK", "result": live_id, "time": "1ms"}])

    async def kill(self, live_id: str) -> None:
        self.killed.append(live_id)
        gate = self.kill_gates.get(live_id)
        if gate is not None:
            await gate.wait()
        self._conn._live_callbacks.pop(live_id, None)


def _notification(live_id: str, record: str) -> dict[str, Any]:
    return {"action": "CREATE", "id": live_id, "result": {"id": record, "tag": "x"}}


async def _stream(conn: WebSocketConnection, **kwargs: Any) -> LiveSelectStream:
    stream = LiveSelectStream(conn, "u255_items", where=WHERE, params=PARAMS, auto_resubscribe=True, **kwargs)
    await stream.start()
    return stream


async def _until(predicate: Any) -> None:
    """Yield to the event loop until ``predicate()`` holds (bounded, no timed sleep)."""

    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=1.0)


async def _drain_callback_tasks(conn: WebSocketConnection) -> None:
    """Let the fire-and-forget tasks (``on_reconnect``, notifications) run."""
    if conn._callback_tasks:
        await asyncio.wait(set(conn._callback_tasks), timeout=1.0)


class TestStreamFollowsTheResubscribe:
    """The stream is moved to the re-created live query during the resubscribe itself."""

    async def test_live_id_is_the_new_id(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        stream = await _stream(conn)

        await conn._resubscribe_all()

        assert stream.live_id == "live-2"
        assert stream.is_active
        # The resubscribe re-sent the same statement with the same bound values.
        assert server.queries[1] == server.queries[0] == ("LIVE SELECT * FROM u255_items WHERE tag = $t", PARAMS)

    async def test_bookkeeping_holds_only_the_new_id(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        _FakeServer(conn, "live-1", "live-2")
        stream = await _stream(conn)

        await conn._resubscribe_all()

        assert conn._live_callbacks == {"live-2": stream._handle_notification}
        assert list(conn._live_subscriptions) == ["live-2"]

    async def test_notification_for_the_new_id_reaches_the_iterator(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        _FakeServer(conn, "live-1", "live-2")
        stream = await _stream(conn)
        await conn._resubscribe_all()

        await conn._process_message(_notification("live-2", "u255_items:after"))

        change = await asyncio.wait_for(anext(stream), timeout=1.0)
        assert change.record_id == "u255_items:after"
        assert change.id == "live-2"

    async def test_a_second_reconnect_moves_it_again(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        _FakeServer(conn, "live-1", "live-2", "live-3")
        stream = await _stream(conn)

        await conn._resubscribe_all()
        await conn._resubscribe_all()

        assert stream.live_id == "live-3"
        assert conn._live_callbacks == {"live-3": stream._handle_notification}
        assert list(conn._live_subscriptions) == ["live-3"]

    async def test_user_on_reconnect_still_fires_with_old_and_new_id(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        _FakeServer(conn, "live-1", "live-2")
        calls: list[tuple[str, str]] = []

        async def on_reconnect(old_id: str, new_id: str) -> None:
            calls.append((old_id, new_id))

        await _stream(conn, on_reconnect=on_reconnect)
        await conn._resubscribe_all()
        await _drain_callback_tasks(conn)

        assert calls == [("live-1", "live-2")]


class TestStopAfterReconnect:
    """``stop()`` after a reconnect ends the re-created live query, not the old one."""

    async def test_stop_kills_the_new_id_and_clears_it(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        stream = await _stream(conn)
        await conn._resubscribe_all()

        await stream.stop()

        assert server.killed == ["live-2"]
        assert conn._live_callbacks == {}
        assert conn._live_subscriptions == {}
        assert stream.live_id is None

    async def test_a_stopped_stream_is_not_recreated_on_the_next_reconnect(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2", "live-3")
        stream = await _stream(conn)
        await conn._resubscribe_all()
        await stream.stop()

        await conn._resubscribe_all()

        assert len(server.queries) == 2  # start + first resubscribe, nothing after stop()
        assert conn._live_subscriptions == {}

    async def test_stop_while_the_query_is_being_recreated(self) -> None:
        """A stream stopped (stop() done) mid-resubscribe does not adopt the new id; that query is killed."""
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        calls: list[tuple[str, str]] = []

        async def on_reconnect(old_id: str, new_id: str) -> None:
            calls.append((old_id, new_id))

        stream = await _stream(conn, on_reconnect=on_reconnect)
        server.before_reply = stream.stop  # stop() runs while the LIVE SELECT is in flight

        await conn._resubscribe_all()
        await _drain_callback_tasks(conn)

        assert stream.live_id is None
        assert server.killed == ["live-1", "live-2"]
        assert conn._live_callbacks == {}
        assert conn._live_subscriptions == {}
        assert calls == []

    async def test_stop_whose_kill_is_pending_when_the_query_is_recreated(self) -> None:
        """stop() is mid-kill of the old id when the re-created LIVE SELECT is answered.

        The stream must already count as stopped while that kill is in flight:
        otherwise it adopts the new id, the kill only reaches the old (dead) id,
        and the re-created live query keeps running on the server.
        """
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        calls: list[tuple[str, str]] = []

        async def on_reconnect(old_id: str, new_id: str) -> None:
            calls.append((old_id, new_id))

        stream = await _stream(conn, on_reconnect=on_reconnect)
        live_reply = asyncio.Event()
        kill_reply = asyncio.Event()
        server.query_gate = live_reply
        server.kill_gates["live-1"] = kill_reply

        resubscribe = asyncio.create_task(conn._resubscribe_all())
        await _until(lambda: len(server.queries) == 2)  # the re-created LIVE SELECT is in flight
        stopper = asyncio.create_task(stream.stop())
        await _until(lambda: server.killed == ["live-1"])  # stop()'s kill of the old id is in flight
        live_reply.set()  # the LIVE SELECT is answered first...
        await asyncio.wait_for(resubscribe, timeout=1.0)
        kill_reply.set()  # ...then the kill of the old id
        await asyncio.wait_for(stopper, timeout=1.0)
        await _drain_callback_tasks(conn)

        assert server.killed == ["live-1", "live-2"]
        assert conn._live_subscriptions == {}
        assert conn._live_callbacks == {}
        assert stream.live_id is None
        assert calls == []
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1.0)


class TestCallbackSubscriptionsUnchanged:
    """A subscription that brings its own callback is re-registered as before."""

    async def test_own_callback_is_registered_under_the_new_id(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        _FakeServer(conn, "live-1", "live-2")

        async def on_change(notification: dict[str, Any]) -> None:
            pass

        sub = LiveSubscriptionParams(
            table="u255_items", where=WHERE, params=PARAMS, diff=False, callback=on_change, on_reconnect=None
        )
        first_id = await conn._resubscribe_one(sub)
        assert first_id == "live-1"

        await conn._resubscribe_all()

        assert conn._live_callbacks == {"live-2": on_change}
        assert conn._live_subscriptions == {"live-2": sub}

    def test_params_constructor_is_unchanged(self) -> None:
        """The exported dataclass keeps its constructor and equality: no new public field."""
        init_fields = [f.name for f in dataclasses.fields(LiveSubscriptionParams) if f.init]
        assert init_fields == ["table", "where", "params", "diff", "callback", "on_reconnect"]
        sub = LiveSubscriptionParams(table="t", where=None, params={}, diff=False, callback=None, on_reconnect=None)
        assert sub == LiveSubscriptionParams("t", None, {}, False, None, None)
