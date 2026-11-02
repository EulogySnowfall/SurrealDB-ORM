"""WebSocket reply size and fail-fast on a dropped socket, against an in-process server.

aiohttp caps a received WebSocket message at 4 MiB unless ``ws_connect()`` is
told otherwise. A larger reply ended the read loop with an ``ERROR`` message,
the socket was dropped and the request waited for its whole timeout: nothing
failed the futures still pending, and no reply for them can ever arrive on the
socket the reconnect opens. The transport now has no cap by default
(``max_message_size=None`` → aiohttp ``max_msg_size=0``, like HTTP) and fails
every pending request at once when the read loop ends.

The server is an ``aiohttp.web`` WebSocket on ``127.0.0.1``, so these tests
need no network (they run under ``--network none``). It speaks the JSON
protocol: each request gets ``{"id": ..., "result": null}`` unless the test's
``on_query`` handler says otherwise.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.exceptions import ConnectionError

MiB = 1024 * 1024

#: What the fake server does with a ``query`` request: reply with a value, or
#: act on the socket (close it, stay silent) and return ``None``.
QueryHandler = Callable[[web.WebSocketResponse, dict[str, Any]], Awaitable[Any]]


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


# --- max_message_size validation (E16) ---------------------------------------


@pytest.mark.parametrize(
    ("value", "error", "message"),
    [
        (0, ValueError, "max_message_size must be >= 1, got 0"),
        (-1, ValueError, "max_message_size must be >= 1, got -1"),
        (True, TypeError, "max_message_size must be an int, got bool"),
        (1.5, TypeError, "max_message_size must be an int, got float"),
    ],
)
def test_max_message_size_is_checked_at_construction(value: object, error: type[Exception], message: str) -> None:
    with pytest.raises(error) as excinfo:
        WebSocketConnection("ws://127.0.0.1:1", "ns", "db", max_message_size=value)  # type: ignore[arg-type]
    assert str(excinfo.value) == message


def test_max_message_size_defaults_to_none() -> None:
    conn = WebSocketConnection("ws://127.0.0.1:1", "ns", "db")
    assert conn.max_message_size is None


# --- max_message_size reaches ws_connect() -----------------------------------


@pytest.mark.parametrize(("value", "expected"), [(None, 0), (8 * MiB, 8 * MiB)])
async def test_max_message_size_is_passed_to_ws_connect_on_connect_and_reconnect(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch, value: int | None, expected: int
) -> None:
    spy = _WsConnectSpy(monkeypatch)
    conn = WebSocketConnection(
        server.url, "ns", "db", protocol="json", timeout=5, reconnect_interval=0.01, max_message_size=value
    )
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


# --- pending requests fail when the socket drops (E15) -----------------------


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

        assert elapsed < 1
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

        assert elapsed < 1
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


# --- require_int lives in the SDK (P1) ---------------------------------------


def test_require_int_is_hosted_by_the_sdk() -> None:
    from surreal_sdk.utils import require_int

    with pytest.raises(TypeError) as type_error:
        require_int("max_message_size", True, minimum=1)
    assert str(type_error.value) == "max_message_size must be an int, got bool"

    with pytest.raises(ValueError) as value_error:
        require_int("max_message_size", 0, minimum=1)
    assert str(value_error.value) == "max_message_size must be >= 1, got 0"

    class Count(int):
        def __format__(self, spec: str) -> str:
            return "many"

    result = require_int("batch_size", Count(3), minimum=1)
    assert result == 3
    assert type(result) is int


def test_orm_require_int_is_the_sdk_one() -> None:
    import src.surreal_orm.utils as orm_utils
    import surreal_sdk.utils as sdk_utils

    assert orm_utils.require_int is sdk_utils.require_int
