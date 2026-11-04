"""Issue #453 — WebSocket reply size and fail-fast on a dropped socket (v2 line).

aiohttp caps a received WebSocket message at 4 MiB unless ``ws_connect()`` is
told otherwise. A larger reply ended the read loop with an ``ERROR`` message,
the socket was dropped and the request waited for its whole timeout: nothing
failed the futures still pending, and no reply for them can ever arrive on the
socket the reconnect opens. The transport now has no cap by default
(``max_message_size=0`` → aiohttp ``max_msg_size=0``, like HTTP) and fails
every pending request at once when the read loop ends.

Two kinds of fake: a fake socket handed straight to ``_read_loop()`` (the
``ERROR`` / ``CLOSE`` cases), and an in-process ``aiohttp.web`` WebSocket
server on ``127.0.0.1`` speaking the JSON protocol, so no test needs a network
(they run under ``--network none``). The server answers each request with
``{"id": ..., "result": null}`` unless the test's ``on_query`` says otherwise.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.exceptions import ConnectionError
from src.surreal_sdk.protocol.rpc import RPCResponse

MiB = 1024 * 1024

#: What the fake server does with a ``query`` request: reply with a value, or
#: act on the socket (close it, stay silent) and return ``None``.
QueryHandler = Callable[[web.WebSocketResponse, dict[str, Any]], Awaitable[Any]]


# --- fake socket for _read_loop() --------------------------------------------


class _FakeSocket:
    """Yields the given messages to ``async for``, then reports an exception and close code."""

    def __init__(
        self, messages: list[aiohttp.WSMessage], exception: BaseException | None = None, close_code: int | None = None
    ) -> None:
        self._messages = messages
        self._exception = exception
        self.close_code = close_code

    async def __aiter__(self) -> AsyncIterator[aiohttp.WSMessage]:
        for message in self._messages:
            yield message

    def exception(self) -> BaseException | None:
        return self._exception


def _with_pending(conn: WebSocketConnection, count: int) -> list[asyncio.Future[RPCResponse]]:
    loop = asyncio.get_running_loop()
    futures: list[asyncio.Future[RPCResponse]] = []
    for request_id in range(1, count + 1):
        future: asyncio.Future[RPCResponse] = loop.create_future()
        conn._pending[request_id] = future
        futures.append(future)
    return futures


async def test_error_message_fails_every_pending_request_with_the_reason() -> None:
    error = aiohttp.WebSocketError(1009, f"Message size 5242880 exceeds limit {4 * MiB}")
    conn = WebSocketConnection("ws://127.0.0.1:1", "ns", "db", auto_reconnect=False)
    conn._ws = _FakeSocket([aiohttp.WSMessage(aiohttp.WSMsgType.ERROR, error, None)])  # type: ignore[assignment]
    futures = _with_pending(conn, 2)

    await conn._read_loop()

    assert conn._pending == {}
    for future in futures:
        with pytest.raises(ConnectionError) as excinfo:
            future.result()
        assert str(excinfo.value) == f"WebSocket connection lost: Message size 5242880 exceeds limit {4 * MiB}"


async def test_close_message_fails_pending_requests_with_the_close_code() -> None:
    conn = WebSocketConnection("ws://127.0.0.1:1", "ns", "db", auto_reconnect=False)
    conn._ws = _FakeSocket([aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, 1011, "")], close_code=1011)  # type: ignore[assignment]
    (future,) = _with_pending(conn, 1)

    await conn._read_loop()

    with pytest.raises(ConnectionError) as excinfo:
        future.result()
    assert str(excinfo.value) == "WebSocket connection lost: closed with code 1011"


async def test_pending_requests_fail_before_the_reconnect_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = WebSocketConnection("ws://127.0.0.1:1", "ns", "db", auto_reconnect=True)
    conn._ws = _FakeSocket([aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)], close_code=1006)  # type: ignore[assignment]
    (future,) = _with_pending(conn, 1)
    seen_at_reconnect: list[bool] = []

    async def fake_reconnect() -> None:
        seen_at_reconnect.append(future.done())

    monkeypatch.setattr(conn, "_reconnect", fake_reconnect)

    await conn._read_loop()
    assert future.done()
    assert conn._reconnect_task is not None
    await conn._reconnect_task

    assert seen_at_reconnect == [True]


async def test_read_loop_leaves_pending_requests_to_close_when_closing() -> None:
    conn = WebSocketConnection("ws://127.0.0.1:1", "ns", "db", auto_reconnect=False)
    conn._closing = True
    conn._ws = _FakeSocket([aiohttp.WSMessage(aiohttp.WSMsgType.CLOSED, None, None)])  # type: ignore[assignment]
    (future,) = _with_pending(conn, 1)

    await conn._read_loop()

    assert not future.done()
    future.cancel()


# --- max_message_size validation ---------------------------------------------


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [
        (-1, ValueError, "max_message_size must be >= 0, got -1"),
        (True, TypeError, "max_message_size must be an int, got bool"),
        (1.5, TypeError, "max_message_size must be an int, got float"),
        ("8", TypeError, "max_message_size must be an int, got str"),
    ],
)
def test_max_message_size_is_checked_at_construction(value: object, error: type[Exception], message: str) -> None:
    with pytest.raises(error) as excinfo:
        WebSocketConnection("ws://127.0.0.1:1", "ns", "db", max_message_size=value)  # type: ignore[arg-type]
    assert str(excinfo.value) == message


def test_max_message_size_defaults_to_no_cap() -> None:
    conn = WebSocketConnection("ws://127.0.0.1:1", "ns", "db")
    assert conn.max_message_size == 0


def test_max_message_size_is_stored_as_a_plain_int() -> None:
    class Size(int):
        pass

    conn = WebSocketConnection("ws://127.0.0.1:1", "ns", "db", max_message_size=Size(8))
    assert conn.max_message_size == 8
    assert type(conn.max_message_size) is int


# --- in-process server --------------------------------------------------------


@dataclass
class FakeServer:
    """An in-process SurrealDB stand-in: answers every RPC, delegates ``query``."""

    on_query: QueryHandler | None = None
    url: str = ""
    connections: int = 0
    sockets: list[web.WebSocketResponse] = field(default_factory=list)

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=["json"], max_msg_size=0)
        await ws.prepare(request)
        self.connections += 1
        self.sockets.append(ws)
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            req = json.loads(msg.data)
            if req.get("method") == "query" and self.on_query is not None:
                result = await self.on_query(ws, req)
                if result is None:
                    continue
                await ws.send_str(json.dumps({"id": req["id"], "result": result}))
            else:
                await ws.send_str(json.dumps({"id": req["id"], "result": None}))
        return ws


@pytest.fixture
async def server() -> AsyncGenerator[FakeServer, None]:
    fake = FakeServer()
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
        for ws in fake.sockets:
            await ws.close()
        await runner.cleanup()


def _query_result(payload: Any) -> list[dict[str, Any]]:
    return [{"status": "OK", "time": "1ms", "result": payload}]


class _WsConnectSpy:
    """Record the keyword arguments of every ``ClientSession.ws_connect()`` call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[dict[str, Any]] = []
        original = aiohttp.ClientSession.ws_connect
        spy = self

        def ws_connect(session: aiohttp.ClientSession, url: str, **kwargs: Any) -> Any:
            spy.calls.append(kwargs)
            return original(session, url, **kwargs)

        monkeypatch.setattr(aiohttp.ClientSession, "ws_connect", ws_connect)


@pytest.mark.parametrize(("value", "expected"), [(None, 0), (0, 0), (8 * MiB, 8 * MiB)])
async def test_max_message_size_is_passed_to_ws_connect_on_connect_and_reconnect(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch, value: int | None, expected: int
) -> None:
    spy = _WsConnectSpy(monkeypatch)
    kwargs: dict[str, Any] = {} if value is None else {"max_message_size": value}
    conn = WebSocketConnection(server.url, "ns", "db", protocol="json", timeout=5, reconnect_interval=0.01, **kwargs)
    try:
        await conn.connect()
        assert len(spy.calls) == 1
        assert spy.calls[0]["max_msg_size"] == expected

        # Drop the socket from the server side: the read loop starts _reconnect().
        await server.sockets[0].close()
        deadline = time.monotonic() + 5
        while server.connections < 2 or not conn.is_connected:
            assert time.monotonic() < deadline, "the connection did not reconnect"
            await asyncio.sleep(0.01)

        assert len(spy.calls) == 2
        assert spy.calls[1]["max_msg_size"] == expected
    finally:
        await conn.close()


@pytest.mark.parametrize("auto_reconnect", [False, True])
async def test_pending_query_fails_at_once_when_the_server_closes_the_socket(server: FakeServer, auto_reconnect: bool) -> None:
    async def close_instead_of_replying(ws: web.WebSocketResponse, req: dict[str, Any]) -> Any:
        await ws.close(code=1011)
        return None

    server.on_query = close_instead_of_replying
    conn = WebSocketConnection(
        server.url, "ns", "db", protocol="json", timeout=30, auto_reconnect=auto_reconnect, reconnect_interval=0.01
    )
    try:
        await conn.connect()
        started = time.monotonic()
        with pytest.raises(ConnectionError) as excinfo:
            await conn.query("SELECT * FROM t")
        elapsed = time.monotonic() - started

        assert elapsed < 5
        assert str(excinfo.value) == "WebSocket connection lost: closed with code 1011"
    finally:
        await conn.close()


async def test_reply_above_the_cap_fails_at_once_naming_the_limit(server: FakeServer) -> None:
    async def big_reply(ws: web.WebSocketResponse, req: dict[str, Any]) -> Any:
        return _query_result("x" * (5 * MiB))

    server.on_query = big_reply
    conn = WebSocketConnection(
        server.url, "ns", "db", protocol="json", timeout=30, auto_reconnect=False, max_message_size=4 * MiB
    )
    try:
        await conn.connect()
        started = time.monotonic()
        with pytest.raises(ConnectionError) as excinfo:
            await conn.query("SELECT * FROM t")
        elapsed = time.monotonic() - started

        assert elapsed < 5
        message = str(excinfo.value)
        assert message.startswith("WebSocket connection lost: ")
        assert str(4 * MiB) in message
    finally:
        await conn.close()


async def test_reply_above_4_mib_is_received_by_default(server: FakeServer) -> None:
    payload = "x" * (5 * MiB)

    async def big_reply(ws: web.WebSocketResponse, req: dict[str, Any]) -> Any:
        return _query_result(payload)

    server.on_query = big_reply
    conn = WebSocketConnection(server.url, "ns", "db", protocol="json", timeout=30, auto_reconnect=False)
    try:
        await conn.connect()
        response = await conn.query("SELECT * FROM t")
        assert response.first_result is not None
        assert response.first_result.result == payload
    finally:
        await conn.close()


async def test_close_with_a_pending_query_keeps_its_message(server: FakeServer) -> None:
    """``close()`` is not a dropped socket: the pending request keeps the old error."""
    replied = asyncio.Event()

    async def never_reply(ws: web.WebSocketResponse, req: dict[str, Any]) -> Any:
        replied.set()
        return None

    server.on_query = never_reply
    conn = WebSocketConnection(server.url, "ns", "db", protocol="json", timeout=30, auto_reconnect=False)
    await conn.connect()
    pending = asyncio.create_task(conn.query("SELECT * FROM t"))
    await asyncio.wait_for(replied.wait(), 5)
    await conn.close()

    with pytest.raises(ConnectionError) as excinfo:
        await pending
    assert str(excinfo.value) == "Request failed: Connection closed"
