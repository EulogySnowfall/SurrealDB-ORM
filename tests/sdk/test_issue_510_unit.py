"""#510: a WebSocket reconnect serves no request before the session is restored.

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

from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.exceptions import ConnectionError

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
            body = {"id": req["id"], "result": [{"status": "OK", "time": "1ms", "result": index}]}
        else:
            body = {"id": req["id"], "result": None}
        if ws.closed:
            return
        sock.replied.append(method)
        await ws.send_str(json.dumps(body))

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
