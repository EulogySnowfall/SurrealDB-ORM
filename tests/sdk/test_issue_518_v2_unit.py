"""Issue #518 — a WebSocket reconnect serves no request before the session is restored (v2 line).

Backport of #510 (fixed on ``main`` by #513).

``_reconnect()`` used to mark the connection connected as soon as the new
socket was open, then send ``authenticate`` and ``use``. A request issued by
another task in that window went out on a socket with no namespace selected
(and possibly unauthenticated), and the server answered with a statement
error such as ``Specify a namespace to use``.

A request issued while the session is being restored now waits until
``use()`` has succeeded on the new socket, or fails with ``ConnectionError``
(restore abandoned, ``close()``, or no session within the request timeout).

The server is an ``aiohttp.web`` WebSocket on ``127.0.0.1`` speaking the JSON
protocol, so the tests need no network. Its replies to ``authenticate`` and
``use`` can be held back (a gate) or turned into an RPC error, per socket.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.exceptions import ConnectionError
from src.surreal_sdk.streaming.live_select import LiveSubscriptionParams

pytestmark = pytest.mark.asyncio


@dataclass
class _Socket:
    """What one server-side socket received, in order, and which replies it sent."""

    received: list[str] = field(default_factory=list)
    replied: list[str] = field(default_factory=list)


@dataclass
class _Server:
    """Answers every RPC; ``use``/``authenticate`` on socket ``n`` can be held or failed."""

    url: str = ""
    sockets: list[_Socket] = field(default_factory=list)
    ws: list[web.WebSocketResponse] = field(default_factory=list)
    #: (socket index, method) -> event the reply waits for
    gates: dict[tuple[int, str], asyncio.Event] = field(default_factory=dict)
    #: (socket index, method) -> RPC error message sent instead of a result
    errors: dict[tuple[int, str], str] = field(default_factory=dict)
    #: (socket index, method): the server closes that socket when such a request arrives
    drops: set[tuple[int, str]] = field(default_factory=set)
    #: (socket index, method): the server closes that socket right after answering it
    close_after: set[tuple[int, str]] = field(default_factory=set)
    #: (socket index, method) -> set when that request arrives
    arrived: dict[tuple[int, str], asyncio.Event] = field(default_factory=dict)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)

    def arrival(self, index: int, method: str) -> asyncio.Event:
        return self.arrived.setdefault((index, method), asyncio.Event())

    async def _reply(self, ws: web.WebSocketResponse, sock: _Socket, index: int, req: dict[str, Any]) -> None:
        method = req["method"]
        gate = self.gates.get((index, method))
        if gate is not None:
            await gate.wait()
        error = self.errors.get((index, method))
        if error is not None:
            body: dict[str, Any] = {"id": req["id"], "error": {"code": -32000, "message": error}}
        elif method == "query":
            sql = req["params"][0] if req.get("params") else ""
            # A LIVE SELECT answers with a live id unique to this request.
            result: Any = f"live-{index}-{req['id']}" if str(sql).startswith("LIVE") else index
            body = {"id": req["id"], "result": [{"status": "OK", "time": "1ms", "result": result}]}
        else:
            body = {"id": req["id"], "result": None}
        if ws.closed:
            return
        sock.replied.append(method)
        await ws.send_str(json.dumps(body))
        if (index, method) in self.close_after:
            await ws.close()

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=["json"], max_msg_size=0)
        await ws.prepare(request)
        index = len(self.sockets)
        sock = _Socket()
        self.sockets.append(sock)
        self.ws.append(ws)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            req = json.loads(msg.data)
            sock.received.append(req["method"])
            self.arrival(index, req["method"]).set()
            if (index, req["method"]) in self.drops:
                await ws.close()
                break
            # Reply from a task: a held reply must not stop the socket from reading.
            task = asyncio.create_task(self._reply(ws, sock, index, req))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return ws


@pytest.fixture
async def server() -> AsyncGenerator[_Server, None]:
    fake = _Server()
    app = web.Application()
    app.router.add_get("/rpc", fake.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    fake.url = f"ws://127.0.0.1:{port}"
    try:
        yield fake
    finally:
        for gate in fake.gates.values():
            gate.set()
        for ws in fake.ws:
            await ws.close()
        await runner.cleanup()


async def _connected(server: _Server, **kwargs: Any) -> WebSocketConnection:
    options: dict[str, Any] = {"protocol": "json", "timeout": 10, "reconnect_interval": 0.01}
    options.update(kwargs)
    conn = WebSocketConnection(server.url, "ns", "db", **options)
    await conn.connect()
    conn._token = "tok"  # what a signin() leaves behind: the restore re-authenticates with it
    return conn


async def _drop_first_socket(server: _Server) -> None:
    """Close socket 0 from the server side: the client's read loop starts _reconnect()."""
    await server.ws[0].close()


async def _turns(n: int = 50) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class TestRequestsWaitForTheRestoredSession:
    async def test_query_during_reconnect_is_not_sent_before_use_completes(self, server: _Server) -> None:
        server.gates[(1, "use")] = asyncio.Event()
        conn = await _connected(server)
        try:
            await _drop_first_socket(server)
            await asyncio.wait_for(server.arrival(1, "use").wait(), 5)

            query = asyncio.create_task(conn.query("RETURN 1"))
            await _turns()
            await asyncio.sleep(0.05)

            # use() has not been answered yet: nothing else may reach socket 1.
            assert server.sockets[1].received == ["authenticate", "use"]
            assert not query.done()

            server.gates[(1, "use")].set()
            response = await asyncio.wait_for(query, 5)
            assert response.first_result.result == 1
            assert server.sockets[1].received == ["authenticate", "use", "query"]
        finally:
            await conn.close()

    async def test_query_during_reconnect_waits_for_authenticate_too(self, server: _Server) -> None:
        server.gates[(1, "authenticate")] = asyncio.Event()
        conn = await _connected(server)
        try:
            await _drop_first_socket(server)
            await asyncio.wait_for(server.arrival(1, "authenticate").wait(), 5)

            query = asyncio.create_task(conn.query("RETURN 1"))
            await _turns()
            await asyncio.sleep(0.05)
            assert server.sockets[1].received == ["authenticate"]

            server.gates[(1, "authenticate")].set()
            response = await asyncio.wait_for(query, 5)
            assert response.first_result.result == 1
            assert server.sockets[1].received == ["authenticate", "use", "query"]
        finally:
            await conn.close()

    async def test_query_issued_before_the_new_socket_is_served_after_the_restore(self, server: _Server) -> None:
        # A long backoff: the query is issued while no new socket exists yet.
        conn = await _connected(server, reconnect_interval=0.3)
        try:
            await _drop_first_socket(server)
            await asyncio.wait_for(_until(lambda: conn._reconnect_task is not None), 5)

            response = await asyncio.wait_for(conn.query("RETURN 1"), 5)
            assert response.first_result.result == 1
            assert server.sockets[1].received[-1] == "query"
            assert server.sockets[1].received.index("use") < server.sockets[1].received.index("query")
        finally:
            await conn.close()


async def _until(predicate: Any) -> None:
    while not predicate():
        await asyncio.sleep(0.005)


class TestFailedRestore:
    @pytest.mark.parametrize("method", ["authenticate", "use"])
    async def test_failed_restore_step_keeps_the_session_closed_and_retries(self, server: _Server, method: str) -> None:
        server.gates[(1, method)] = asyncio.Event()
        server.errors[(1, method)] = "Specify a namespace to use" if method == "use" else "Token expired"
        conn = await _connected(server)
        try:
            await _drop_first_socket(server)
            await asyncio.wait_for(server.arrival(1, method).wait(), 5)

            query = asyncio.create_task(conn.query("RETURN 1"))
            await _turns()
            server.gates[(1, method)].set()

            # The failed restore is abandoned and the next attempt opens socket 2.
            response = await asyncio.wait_for(query, 5)
            assert response.first_result.result == 2
            assert "query" not in server.sockets[1].received
            assert server.sockets[2].received == ["authenticate", "use", "query"]
            assert conn.is_connected
        finally:
            await conn.close()

    async def test_callers_get_connection_error_once_every_attempt_failed(self, server: _Server) -> None:
        conn = await _connected(server, max_reconnect_attempts=2, timeout=30)
        for index in (1, 2):
            server.gates[(index, "use")] = asyncio.Event()
            server.errors[(index, "use")] = "Specify a namespace to use"
        try:
            await _drop_first_socket(server)
            await asyncio.wait_for(server.arrival(1, "use").wait(), 5)

            query = asyncio.create_task(conn.query("RETURN 1"))
            await _turns()
            started = time.monotonic()
            server.gates[(1, "use")].set()
            await asyncio.wait_for(server.arrival(2, "use").wait(), 5)
            server.gates[(2, "use")].set()

            with pytest.raises(ConnectionError):
                await asyncio.wait_for(query, 5)
            assert time.monotonic() - started < 5, "the caller waited for its whole timeout"
            assert all("query" not in sock.received for sock in server.sockets[1:])
            assert not conn.is_connected

            # A later request fails at once too.
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(conn.query("RETURN 1"), 1)
        finally:
            await conn.close()

    async def test_readiness_wait_is_bounded_by_the_timeout(self, server: _Server) -> None:
        server.gates[(1, "use")] = asyncio.Event()
        conn = await _connected(server, timeout=0.3)
        try:
            await _drop_first_socket(server)
            await asyncio.wait_for(server.arrival(1, "use").wait(), 5)
            started = time.monotonic()
            with pytest.raises(ConnectionError, match="not restored"):
                await asyncio.wait_for(conn.query("RETURN 1"), 5)
            assert time.monotonic() - started < 2
            assert "query" not in server.sockets[1].received
        finally:
            await conn.close()


class TestCloseDuringReconnect:
    async def test_close_wakes_a_caller_waiting_for_the_session(self, server: _Server) -> None:
        server.gates[(1, "use")] = asyncio.Event()
        conn = await _connected(server, timeout=30)
        await _drop_first_socket(server)
        await asyncio.wait_for(server.arrival(1, "use").wait(), 5)

        query = asyncio.create_task(conn.query("RETURN 1"))
        await _turns()
        assert not query.done()

        await asyncio.wait_for(conn.close(), 5)
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(query, 5)
        assert "query" not in server.sockets[1].received


class TestResubscribeAfterRestore:
    async def test_resubscribe_runs_after_use_completed(self, server: _Server) -> None:
        conn = await _connected(server)
        seen: list[list[str]] = []
        done = asyncio.Event()

        async def resubscribe_all() -> None:
            seen.append(list(server.sockets[1].replied))
            # A resubscription goes through the public path: it must not wait on itself.
            await conn.query("LIVE SELECT * FROM t")
            done.set()

        conn._resubscribe_all = resubscribe_all  # type: ignore[method-assign]
        try:
            await _drop_first_socket(server)
            await asyncio.wait_for(done.wait(), 5)
            assert seen == [["authenticate", "use"]]
            assert server.sockets[1].received == ["authenticate", "use", "query"]
        finally:
            await conn.close()

    async def test_socket_lost_during_resubscribe_retries_at_once(self, server: _Server) -> None:
        # The restore must never wait on itself: with the socket gone again during the
        # first re-created live query, each remaining one used to wait a full timeout
        # for a session only this same reconnect task could restore.
        timeout = 5.0
        server.drops.add((1, "query"))
        conn = await _connected(server, timeout=timeout)
        for i in range(3):
            conn._register_live_subscription(f"old{i}", LiveSubscriptionParams("t", None, {}, False, None, None))
        try:
            started = time.monotonic()
            await _drop_first_socket(server)
            await asyncio.wait_for(server.arrival(1, "query").wait(), 5)
            # The client has seen socket 1 end: the user query cannot go out on it.
            await asyncio.wait_for(_until(lambda: not conn._session_ready.is_set()), 5)

            response = await asyncio.wait_for(conn.query("RETURN 1"), timeout)
            assert response.first_result.result == 2
            await asyncio.wait_for(
                _until(lambda: len(conn._live_subscriptions) == 3 and server.sockets[2].received.count("query") == 4), 5
            )
            assert time.monotonic() - started < timeout / 2, "the next attempt waited for the request timeout"

            # Every subscription is re-created on socket 2, none lost with socket 1.
            assert all(live_id.startswith("live-2-") for live_id in conn._live_subscriptions)
            assert server.sockets[2].received[:2] == ["authenticate", "use"]
        finally:
            await conn.close()


class TestSocketEndsRightAfterTheRestore:
    async def test_reply_then_close_never_marks_the_dead_socket_ready(self, server: _Server) -> None:
        # Socket 1 answers use() and closes at once: its reader ends (and, seeing a
        # reconnect running, leaves the retry to it) before the restore resumes.
        server.close_after.add((1, "use"))
        server.gates[(2, "use")] = asyncio.Event()
        conn = await _connected(server, timeout=10)
        conn._register_live_subscription("old0", LiveSubscriptionParams("t", None, {}, False, None, None))

        restore_rpc = conn._restore_rpc

        async def restore_after_reader_ended(method: str, params: list[Any]) -> Any:
            result = await restore_rpc(method, params)
            if method == "use" and len(server.sockets) == 2:
                # Make the order deterministic: the reply was read, then the close.
                reader = conn._reader_task
                assert reader is not None
                await asyncio.wait_for(asyncio.wait({reader}), 5)
            return result

        conn._restore_rpc = restore_after_reader_ended  # type: ignore[method-assign]
        try:
            await _drop_first_socket(server)
            await asyncio.wait_for(server.arrival(2, "use").wait(), 5)

            query = asyncio.create_task(conn.query("RETURN 1"))
            await _turns()
            await asyncio.sleep(0.05)

            # use() is still held on socket 2: nothing else may reach it.
            assert server.sockets[2].received == ["authenticate", "use"]
            assert not query.done()

            server.gates[(2, "use")].set()
            response = await asyncio.wait_for(query, 5)
            assert response.first_result.result == 2

            # The live subscription is re-created on socket 2, not dropped with socket 1.
            await asyncio.wait_for(_until(lambda: len(conn._live_subscriptions) == 1), 5)
            assert all(live_id.startswith("live-2-") for live_id in conn._live_subscriptions)
            assert "query" not in server.sockets[1].received
        finally:
            await conn.close()
