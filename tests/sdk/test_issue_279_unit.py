"""Unit tests for #279: live queries across a WebSocket reconnect, and naive datetimes in record ids.

1. ``LiveSelectManager`` keyed each stream by the id ``watch()`` returned. Since
   the stream follows the re-created live query after a reconnect,
   ``stop(new_id)`` found nothing and ``active_streams`` listed a dead id.
2. ``WebSocketConnection.live(table, callback)`` and ``LiveQuery.subscribe()``
   registered no ``LiveSubscriptionParams``, so a reconnect never re-created
   them: their callback received nothing more, with no error.
3. ``_resubscribe_all()`` dropped a subscription whose ``LIVE SELECT`` failed
   on a live session ("skip this one"): no later reconnect retried it.
4. A naive ``datetime`` inside a ``RecordId`` id was read as UTC without the
   ``RuntimeWarning`` a naive ``datetime`` anywhere else gets.
5. aiohttp hands the read loop a buffered message without yielding. When the
   ``LIVE SELECT`` reply and the first notification for the new id arrive in
   one read, the notification was processed before the caller had the id, and
   dropped.

Items 1 to 3 use a real ``WebSocketConnection`` that is never connected:
``query()`` and ``rpc()`` are replaced at the method level, and each test
drives ``_resubscribe_all()`` directly, which is what the reconnect path runs
once the new socket is up. Item 5 runs the real read loop on a scripted socket.
"""

from __future__ import annotations

import asyncio
import json
import threading
import warnings
from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import aiohttp
import cbor2
import pytest
from cbor2 import CBORTag

from surreal_sdk.connection import websocket as websocket_module
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.protocol import cbor
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.streaming.live_query import LiveNotification, LiveQuery, LiveQueryManager
from surreal_sdk.streaming.live_select import LiveChange, LiveSelectManager, LiveSelectStream
from surreal_sdk.types import QueryResponse

TABLE = "u279_items"


class _FakeServer:
    """Answers each ``LIVE SELECT`` with the next live id (``None``: a statement error) and records kills."""

    def __init__(self, conn: WebSocketConnection, *replies: str | None) -> None:
        self._replies = list(replies)
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.killed: list[str] = []
        #: Awaited once, while the next LIVE SELECT is in flight (before its reply).
        self.before_reply: Any = None
        conn.query = self.query  # type: ignore[method-assign]
        conn.rpc = self.rpc  # type: ignore[method-assign]

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        self.queries.append((sql, vars))
        reply = self._replies.pop(0)
        if self.before_reply is not None:
            hook, self.before_reply = self.before_reply, None
            await hook()
        if reply is None:
            return QueryResponse.from_rpc_result([{"status": "ERR", "result": "The table is gone", "time": "1ms"}])
        return QueryResponse.from_rpc_result([{"status": "OK", "result": reply, "time": "1ms"}])

    async def rpc(self, method: str, params: Any = None) -> Any:
        assert method == "kill", method
        self.killed.append(params[0])
        return None


def _notification(live_id: str, record: str) -> dict[str, Any]:
    return {"action": "CREATE", "id": live_id, "result": {"id": record, "tag": "x"}}


async def _drain_callback_tasks(conn: WebSocketConnection) -> None:
    """Let the fire-and-forget tasks (``on_reconnect``, notifications) run."""
    for _ in range(3):
        if conn._callback_tasks:
            await asyncio.wait(set(conn._callback_tasks), timeout=1.0)


class _Recorder:
    """An async callback that keeps what it receives."""

    def __init__(self) -> None:
        self.received: list[Any] = []

    async def __call__(self, item: Any) -> None:
        self.received.append(item)


# --------------------------------------------------------------------------- item 1


class TestManagerFollowsTheReconnect:
    async def _watched(self) -> tuple[WebSocketConnection, _FakeServer, LiveSelectManager, _Recorder]:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2", "live-3")
        manager = LiveSelectManager(conn)
        recorder = _Recorder()
        assert await manager.watch(TABLE, recorder) == "live-1"
        await conn._resubscribe_all()
        return conn, server, manager, recorder

    async def test_active_streams_lists_the_new_id(self) -> None:
        _, _, manager, _ = await self._watched()
        assert manager.active_streams == ["live-2"]
        assert manager.count == 1

    async def test_stop_with_the_new_id_kills_it_and_removes_the_stream(self) -> None:
        conn, server, manager, _ = await self._watched()

        await manager.stop("live-2")

        assert server.killed == ["live-2"]
        assert manager.active_streams == []
        assert conn._live_subscriptions == {}
        assert conn._live_callbacks == {}
        # Not re-created by a later reconnect.
        await conn._resubscribe_all()
        assert len(server.queries) == 2

    async def test_stop_with_the_id_watch_returned_still_works(self) -> None:
        conn, server, manager, _ = await self._watched()

        await manager.stop("live-1")

        assert server.killed == ["live-2"]
        assert manager.active_streams == []
        assert conn._live_subscriptions == {}

    async def test_unknown_id_does_nothing(self) -> None:
        _, server, manager, _ = await self._watched()

        await manager.stop("live-9")

        assert server.killed == []
        assert manager.active_streams == ["live-2"]

    async def test_events_for_the_new_id_reach_the_callback(self) -> None:
        conn, _, manager, recorder = await self._watched()

        await conn._process_message(_notification("live-2", f"{TABLE}:after"))
        await asyncio.wait_for(_until(lambda: recorder.received), 1.0)

        assert [change.record_id for change in recorder.received] == [f"{TABLE}:after"]
        await manager.stop_all()


class TestLiveQueryManagerFollowsTheReconnect:
    async def test_unwatch_with_the_new_or_first_id(self) -> None:
        for stop_with in ("live-1", "live-2"):
            conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
            server = _FakeServer(conn, "live-1", "live-2")
            manager = LiveQueryManager(conn)
            assert await manager.watch(TABLE, _Recorder()) == "live-1"
            await conn._resubscribe_all()
            assert manager.active_queries == ["live-2"]

            await manager.unwatch(stop_with)

            assert server.killed == ["live-2"], stop_with
            assert manager.active_queries == []
            assert conn._live_subscriptions == {}


# --------------------------------------------------------------------------- item 2


class TestCallbackLiveIsResubscribed:
    async def test_live_registers_a_subscription_with_its_callback(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        recorder = _Recorder()

        assert await conn.live(TABLE, recorder, diff=True) == "live-1"
        await conn._resubscribe_all()

        assert server.queries[1] == server.queries[0] == (f"LIVE SELECT DIFF FROM {TABLE}", None)
        assert conn._live_callbacks == {"live-2": recorder}
        assert list(conn._live_subscriptions) == ["live-2"]
        assert conn.live_queries == ["live-2"]

        await conn._process_message(_notification("live-2", f"{TABLE}:after"))
        await _drain_callback_tasks(conn)
        assert [item["result"]["id"] for item in recorder.received] == [f"{TABLE}:after"]

    async def test_kill_with_the_returned_id_ends_the_recreated_query(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2", "live-3")
        first = await conn.live(TABLE, _Recorder())
        await conn._resubscribe_all()

        await conn.kill(first)

        assert server.killed == ["live-2"]
        assert conn._live_subscriptions == {}
        assert conn._live_callbacks == {}
        await conn._resubscribe_all()
        assert len(server.queries) == 2

    async def test_kill_with_the_returned_id_after_two_reconnects(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2", "live-3")
        first = await conn.live(TABLE, _Recorder())
        await conn._resubscribe_all()
        await conn._resubscribe_all()
        assert conn._live_id_aliases == {"live-1": "live-3"}

        await conn.kill(first)

        assert server.killed == ["live-3"]
        assert conn._live_id_aliases == {}
        assert conn._live_subscriptions == {}

    async def test_kill_with_the_current_id(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2", "live-3")
        await conn.live(TABLE, _Recorder())
        await conn._resubscribe_all()

        await conn.kill("live-2")

        assert server.killed == ["live-2"]
        assert conn._live_subscriptions == {}
        await conn._resubscribe_all()
        assert len(server.queries) == 2

    async def test_killed_before_any_reconnect_is_never_recreated(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        live_id = await conn.live(TABLE, _Recorder())

        await conn.kill(live_id)
        await conn._resubscribe_all()

        assert len(server.queries) == 1
        assert conn._live_subscriptions == {}

    async def test_kill_whose_request_fails_still_unregisters(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        live_id = await conn.live(TABLE, _Recorder())

        async def failing_rpc(method: str, params: Any = None) -> Any:
            raise ConnectionError("socket gone")

        conn.rpc = failing_rpc  # type: ignore[method-assign]
        with pytest.raises(ConnectionError):
            await conn.kill(live_id)
        conn.rpc = server.rpc  # type: ignore[method-assign]
        await conn._resubscribe_all()

        assert len(server.queries) == 1
        assert conn._live_subscriptions == {}

    async def test_kill_while_the_query_is_being_recreated(self) -> None:
        """kill() with the old id while the re-created LIVE SELECT is in flight ends the new query too."""
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        recorder = _Recorder()
        first = await conn.live(TABLE, recorder)

        async def kill_first() -> None:
            await conn.kill(first)

        server.before_reply = kill_first
        await conn._resubscribe_all()

        assert server.killed == ["live-1", "live-2"]
        assert conn._live_subscriptions == {}
        assert conn._live_callbacks == {}

    async def test_kill_all_also_ends_subscriptions_waiting_for_a_retry(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", None, "live-3")
        await conn.live(TABLE, _Recorder())
        await conn._resubscribe_all()  # fails: kept under live-1, no handler
        assert list(conn._live_subscriptions) == ["live-1"]

        await conn.kill_all_live_queries()
        await conn._resubscribe_all()

        assert conn._live_subscriptions == {}
        assert len(server.queries) == 2


class TestLiveQueryIsResubscribed:
    async def test_live_query_follows_the_new_id(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        recorder = _Recorder()
        live = LiveQuery(conn, TABLE, where="tag = 'x'")

        assert await live.subscribe(recorder) == "live-1"
        await conn._resubscribe_all()

        assert live.live_id == "live-2"
        assert live.is_active
        assert server.queries[1] == server.queries[0] == (f"LIVE SELECT * FROM {TABLE} WHERE tag = 'x'", None)
        await conn._process_message(_notification("live-2", f"{TABLE}:after"))
        await _drain_callback_tasks(conn)
        assert len(recorder.received) == 1
        notification = recorder.received[0]
        assert isinstance(notification, LiveNotification)
        assert notification.id == "live-2"

    async def test_unsubscribe_after_the_reconnect_kills_the_new_id(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2", "live-3")
        live = LiveQuery(conn, TABLE)
        await live.subscribe(_Recorder())
        await conn._resubscribe_all()

        await live.unsubscribe()

        assert server.killed == ["live-2"]
        assert conn._live_subscriptions == {}
        assert conn._live_callbacks == {}
        await conn._resubscribe_all()
        assert len(server.queries) == 2

    async def test_unsubscribe_while_the_query_is_being_recreated(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        live = LiveQuery(conn, TABLE)
        await live.subscribe(_Recorder())
        server.before_reply = live.unsubscribe

        await conn._resubscribe_all()

        assert server.killed == ["live-1", "live-2"]
        assert live.live_id is None
        assert conn._live_subscriptions == {}
        assert conn._live_callbacks == {}

    async def test_unsubscribe_unregisters_before_its_kill(self) -> None:
        """A kill still in flight when the reconnect re-creates queries leaves nothing to re-create."""
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", "live-2")
        live = LiveQuery(conn, TABLE)
        await live.subscribe(_Recorder())
        gate = asyncio.Event()

        async def held_kill(method: str, params: Any = None) -> Any:
            await gate.wait()
            return await server.rpc(method, params)

        conn.rpc = held_kill  # type: ignore[method-assign]
        stopper = asyncio.create_task(live.unsubscribe())
        await asyncio.sleep(0)
        await conn._resubscribe_all()
        gate.set()
        await asyncio.wait_for(stopper, 1.0)

        assert len(server.queries) == 1
        assert conn._live_subscriptions == {}


# --------------------------------------------------------------------------- item 3


class TestFailedResubscribeIsRetried:
    async def test_next_reconnect_recreates_the_stream(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", None, "live-3")
        reconnects: list[tuple[str, str]] = []

        async def on_reconnect(old_id: str, new_id: str) -> None:
            reconnects.append((old_id, new_id))

        stream = LiveSelectStream(conn, TABLE, where="tag = $t", params={"t": "x"}, on_reconnect=on_reconnect)
        await stream.start()

        await conn._resubscribe_all()  # the server answers the LIVE SELECT with an error
        assert conn._session_ready.is_set()
        assert stream.live_id == "live-1"
        assert list(conn._live_subscriptions) == ["live-1"]

        await conn._resubscribe_all()
        await _drain_callback_tasks(conn)

        assert stream.live_id == "live-3"
        assert list(conn._live_subscriptions) == ["live-3"]
        assert reconnects == [("live-1", "live-3")]
        assert server.queries[2] == server.queries[0] == (f"LIVE SELECT * FROM {TABLE} WHERE tag = $t", {"t": "x"})
        await conn._process_message(_notification("live-3", f"{TABLE}:after"))
        change = await asyncio.wait_for(anext(stream), 1.0)
        assert change.record_id == f"{TABLE}:after"

    async def test_a_stream_stopped_in_between_is_not_recreated(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        server = _FakeServer(conn, "live-1", None, "live-3")
        stream = LiveSelectStream(conn, TABLE)
        await stream.start()
        await conn._resubscribe_all()

        await stream.stop()
        await conn._resubscribe_all()

        assert len(server.queries) == 2
        assert conn._live_subscriptions == {}

    async def test_others_are_still_recreated_after_one_fails(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        _FakeServer(conn, "live-a", "live-b", None, "live-b2", "live-a3")
        first = LiveSelectStream(conn, TABLE)
        second = LiveSelectStream(conn, TABLE)
        await first.start()
        await second.start()

        await conn._resubscribe_all()  # first fails, second moves to live-b2
        assert (first.live_id, second.live_id) == ("live-a", "live-b2")
        assert set(conn._live_subscriptions) == {"live-a", "live-b2"}

        _FakeServer(conn, "live-a3", "live-b3")
        await conn._resubscribe_all()
        assert (first.live_id, second.live_id) == ("live-a3", "live-b3")
        assert set(conn._live_subscriptions) == {"live-a3", "live-b3"}


# --------------------------------------------------------------------------- item 4

NAIVE = datetime(2026, 2, 1, 12, 30)
WARNING = "A naive datetime was sent to SurrealDB"
SECONDS = int(NAIVE.replace(tzinfo=UTC).timestamp())


def _dt_tag() -> CBORTag:
    return CBORTag(12, [SECONDS, 0])


#: Each record id, and the bytes it was encoded to before the fix (as {"id": <record id>}).
RECORD_IDS: dict[str, tuple[RecordId, bytes]] = {
    "array": (
        RecordId("t", ["x", NAIVE]),
        bytes.fromhex("a1626964c8826174826178cc821a697f474800"),
    ),
    "object": (
        RecordId("t", {"at": NAIVE}),
        bytes.fromhex("a1626964c8826174a1626174cc821a697f474800"),
    ),
    "nested": (
        RecordId("t", [{"k": [("a", NAIVE)]}]),
        bytes.fromhex("a1626964c882617481a1616b81826161cc821a697f474800"),
    ),
}


class TestNaiveDatetimeInRecordId:
    @pytest.mark.parametrize("name", list(RECORD_IDS))
    def test_warns(self, name: str) -> None:
        record_id, _ = RECORD_IDS[name]
        with pytest.warns(RuntimeWarning, match=WARNING):
            cbor.encode({"id": record_id})

    @pytest.mark.parametrize("name", list(RECORD_IDS))
    def test_raises_under_the_documented_error_filter(self, name: str) -> None:
        record_id, _ = RECORD_IDS[name]
        with warnings.catch_warnings():
            warnings.filterwarnings("error", message=WARNING, category=RuntimeWarning)
            with pytest.raises(RuntimeWarning, match=WARNING):
                cbor.encode({"id": record_id})

    @pytest.mark.parametrize("name", list(RECORD_IDS))
    def test_bytes_are_unchanged(self, name: str) -> None:
        record_id, before = RECORD_IDS[name]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            encoded = cbor.encode({"id": record_id})
        assert encoded == before

    def test_bytes_are_the_tagged_record_id(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            encoded = cbor.encode(RecordId("t", ["x", NAIVE]))
        assert encoded == cbor2.dumps(CBORTag(8, ["t", ["x", _dt_tag()]]))

    def test_record_id_inside_a_record_id(self) -> None:
        with pytest.warns(RuntimeWarning, match=WARNING):
            cbor.encode([RecordId("t", [RecordId("u", [NAIVE])])])

    def test_aware_datetime_does_not_warn(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            cbor.encode({"id": RecordId("t", ["x", NAIVE.replace(tzinfo=UTC)])})

    def test_plain_ids_do_not_warn(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            cbor.encode([RecordId("t", "abc"), RecordId("t", 1), RecordId("t", ["a", {"b": 2}])])

    def test_record_id_is_not_rewritten(self) -> None:
        record_id = RecordId("t", ["x", NAIVE, None])
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            assert cbor._preprocess_for_cbor(record_id) is record_id
        assert record_id.id == ["x", NAIVE, None]


def _cyclic_list() -> list[Any]:
    cyclic: list[Any] = ["x"]
    cyclic.append(cyclic)
    return cyclic


def _cyclic_dict() -> dict[str, Any]:
    cyclic: dict[str, Any] = {"a": 1}
    cyclic["self"] = cyclic
    return cyclic


def _cyclic_through_record_id() -> list[Any]:
    cyclic: list[Any] = ["x"]
    cyclic.append(RecordId("u", cyclic))
    return cyclic


CYCLIC_IDS = {
    "list": _cyclic_list,
    "dict": _cyclic_dict,
    "through_a_record_id": _cyclic_through_record_id,
}


def _bounded(call: Any, *args: Any, timeout: float = 5.0) -> BaseException | None:
    """Run ``call(*args)`` in a daemon thread and return what it raised (``None`` if nothing).

    The record id walk is synchronous, so a cycle it does not detect never
    returns: the thread bounds the test instead of hanging the whole run.
    Warnings are recorded by the caller's ``catch_warnings`` (module state).
    """
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            call(*args)
        except BaseException as exc:
            outcome.append(exc)
        else:
            outcome.append(None)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"{call.__name__}() did not return: the record id walk loops on a cycle"
    return outcome[0]


class TestCyclicRecordIdStillRaises:
    """The walk must not turn cbor2's cycle error into a hang (review of #279)."""

    @pytest.mark.parametrize("name", list(CYCLIC_IDS))
    def test_cbor2_still_refuses_the_cycle(self, name: str) -> None:
        raised = _bounded(cbor.encode, {"id": RecordId("t", CYCLIC_IDS[name]())})
        assert isinstance(raised, cbor2.CBOREncodeValueError)
        assert "cyclic data structure" in str(raised)

    @pytest.mark.parametrize("name", list(CYCLIC_IDS))
    def test_the_walk_returns_without_a_warning(self, name: str) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            assert _bounded(cbor._warn_naive_in_record_id, CYCLIC_IDS[name]()) is None

    def test_a_naive_datetime_in_a_cyclic_id_still_warns(self) -> None:
        cyclic: list[Any] = ["x", {"k": [NAIVE]}]
        cyclic.append(cyclic)
        with pytest.warns(RuntimeWarning, match=WARNING):
            raised = _bounded(cbor.encode, {"id": RecordId("t", cyclic)})
        assert isinstance(raised, cbor2.CBOREncodeValueError)

    def test_a_container_shared_twice_is_still_walked(self) -> None:
        shared = [{"at": NAIVE}]
        with pytest.warns(RuntimeWarning, match=WARNING):
            cbor.encode({"id": RecordId("t", ["x", shared, shared])})

    def test_a_nested_record_id_still_warns(self) -> None:
        with pytest.warns(RuntimeWarning, match=WARNING):
            cbor.encode({"id": RecordId("t", [RecordId("u", {"at": NAIVE})])})


# --------------------------------------------------------------------------- item 5


class _ScriptedSocket:
    """Stands in for aiohttp's ``ClientWebSocketResponse`` (JSON protocol).

    A message already buffered is handed to the read loop without yielding, as
    aiohttp's ``WebSocketDataQueue.read()`` does, so messages pushed together
    are processed in one go. ``on_live_select(live_id, reply)`` returns the
    messages a ``LIVE SELECT`` is answered with, in that order.
    """

    def __init__(self, *live_ids: str) -> None:
        self._live_ids = list(live_ids)
        self._buffer: deque[str] = deque()
        self._wakeup = asyncio.Event()
        self.closed = False
        self.close_code: int | None = None
        self.on_live_select: Any = lambda live_id, reply: [reply]

    def push(self, *messages: dict[str, Any]) -> None:
        self._buffer.extend(json.dumps(message) for message in messages)
        self._wakeup.set()

    async def send_str(self, data: str) -> None:
        request = json.loads(data)
        if request["method"] == "query" and str(request["params"][0]).startswith("LIVE SELECT"):
            live_id = self._live_ids.pop(0)
            reply = {"id": request["id"], "result": [{"status": "OK", "time": "1ms", "result": live_id}]}
            self.push(*self.on_live_select(live_id, reply))
        else:
            self.push({"id": request["id"], "result": None})

    def __aiter__(self) -> _ScriptedSocket:
        return self

    async def __anext__(self) -> Any:
        while not self._buffer:
            if self.closed:
                raise StopAsyncIteration
            self._wakeup.clear()
            await self._wakeup.wait()
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=self._buffer.popleft())

    async def close(self) -> None:
        self.closed = True
        self._wakeup.set()

    def exception(self) -> BaseException | None:
        return None


def _wrapped(live_id: str, record: str) -> dict[str, Any]:
    """A notification as SurrealDB sends it: wrapped in ``result``, with no request id."""
    return {"result": _notification(live_id, record)}


async def _scripted(sock: _ScriptedSocket) -> WebSocketConnection:
    conn = WebSocketConnection("ws://localhost:8000", "ns", "db", protocol="json", auto_reconnect=False, timeout=5)
    conn._ws = sock  # type: ignore[assignment]
    conn._connected = True
    conn._reader_task = asyncio.create_task(conn._read_loop())
    return conn


async def _until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0)


def _with_first_event(live_id: str, reply: dict[str, Any]) -> list[dict[str, Any]]:
    return [reply, _wrapped(live_id, f"{TABLE}:first")]


class TestNotificationInTheSameReadAsTheReply:
    async def test_first_start(self) -> None:
        sock = _ScriptedSocket("live-1")
        sock.on_live_select = _with_first_event
        conn = await _scripted(sock)
        try:
            stream = LiveSelectStream(conn, TABLE)
            await stream.start()

            change = await asyncio.wait_for(anext(stream), 1.0)
            assert isinstance(change, LiveChange)
            assert (change.id, change.record_id) == ("live-1", f"{TABLE}:first")
            assert len(conn._held_notifications) == 0
        finally:
            await conn.close()

    async def test_resubscribe(self) -> None:
        sock = _ScriptedSocket("live-1", "live-2")
        conn = await _scripted(sock)
        try:
            stream = LiveSelectStream(conn, TABLE)
            await stream.start()
            sock.on_live_select = _with_first_event

            await conn._resubscribe_all()

            change = await asyncio.wait_for(anext(stream), 1.0)
            assert (change.id, change.record_id) == ("live-2", f"{TABLE}:first")
            assert stream.live_id == "live-2"
        finally:
            await conn.close()

    async def test_callback_live_and_live_query(self) -> None:
        sock = _ScriptedSocket("live-1", "live-2")
        sock.on_live_select = _with_first_event
        conn = await _scripted(sock)
        try:
            raw = _Recorder()
            await conn.live(TABLE, raw)
            typed = _Recorder()
            await LiveQuery(conn, TABLE).subscribe(typed)

            await asyncio.wait_for(_until(lambda: raw.received and typed.received), 1.0)
            assert raw.received[0]["id"] == "live-1"
            assert typed.received[0].id == "live-2"
        finally:
            await conn.close()

    async def test_order_is_kept(self) -> None:
        sock = _ScriptedSocket("live-1")
        sock.on_live_select = lambda live_id, reply: [
            reply,
            _wrapped(live_id, f"{TABLE}:a"),
            _wrapped(live_id, f"{TABLE}:b"),
        ]
        conn = await _scripted(sock)
        try:
            stream = LiveSelectStream(conn, TABLE)
            await stream.start()
            sock.push(_wrapped("live-1", f"{TABLE}:c"))

            records = [(await asyncio.wait_for(anext(stream), 1.0)).record_id for _ in range(3)]
            assert records == [f"{TABLE}:a", f"{TABLE}:b", f"{TABLE}:c"]
        finally:
            await conn.close()

    async def test_unknown_ids_are_dropped_with_a_bounded_buffer(self) -> None:
        limit = websocket_module._HELD_LIVE_NOTIFICATIONS_MAX
        held_reply: list[dict[str, Any]] = []
        sock = _ScriptedSocket("live-1")

        def ghosts_then_hold_the_reply(live_id: str, reply: dict[str, Any]) -> list[dict[str, Any]]:
            held_reply.append(reply)
            return [_wrapped(f"ghost-{i}", f"{TABLE}:{i}") for i in range(limit * 4)]

        sock.on_live_select = ghosts_then_hold_the_reply
        conn = await _scripted(sock)
        try:
            stream = LiveSelectStream(conn, TABLE)
            start = asyncio.create_task(stream.start())
            await asyncio.wait_for(_until(lambda: held_reply and not sock._buffer), 1.0)

            # While the LIVE SELECT is in flight, at most the bound is kept: the newest.
            assert len(conn._held_notifications) == limit
            assert conn._held_notifications[0][0] == f"ghost-{limit * 3}"

            sock.push(held_reply[0])
            await asyncio.wait_for(start, 1.0)

            # Nothing in flight any more: nothing is kept, nothing was delivered.
            assert len(conn._held_notifications) == 0
            assert stream._queue.empty()
            assert conn._callback_tasks == set()

            # And with nothing in flight, an unknown id is not held at all.
            sock.push(_wrapped("ghost-late", f"{TABLE}:late"))
            await asyncio.wait_for(_until(lambda: not sock._buffer), 1.0)
            await asyncio.sleep(0)
            assert len(conn._held_notifications) == 0
        finally:
            await conn.close()

    async def test_failed_live_select_drops_what_it_held(self) -> None:
        sock = _ScriptedSocket("live-1")
        sock.on_live_select = lambda live_id, reply: [
            _wrapped(live_id, f"{TABLE}:x"),
            {"id": reply["id"], "error": {"code": -32000, "message": "refused"}},
        ]
        conn = await _scripted(sock)
        try:
            with pytest.raises(Exception, match="refused"):
                await LiveSelectStream(conn, TABLE).start()
            assert conn._live_selects_in_flight == 0
            assert len(conn._held_notifications) == 0
        finally:
            await conn.close()
