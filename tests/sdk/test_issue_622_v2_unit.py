"""Issue #622 — ``ConnectionPool.acquire()`` closes a connection whose initialisation fails (v2 line).

Backport of #614 (fixed on ``main`` by #621).

``acquire()`` builds a new connection and initialises it (``connect()``, then
the pool sign-in). When that raised or was cancelled, the semaphore slot was
released but the connection, already opened by ``connect()``, was left open:
nothing referenced it any more and nothing closed it.

Most connections here are real ``HTTPConnection`` / ``WebSocketConnection``
objects whose ``connect()``, ``signin()`` and ``close()`` are replaced by
mocks. ``TestI622WebSocketConnectCancelled`` runs the real
``WebSocketConnection.connect()`` / ``close()`` over a fake aiohttp session:
on this branch ``connect()`` releases what it opened on an ``Exception``, not
on a cancellation, so only the pool's close releases it then. No server is
needed.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
import pytest

from src.surreal_sdk.connection.base import BaseSurrealConnection
from src.surreal_sdk.connection.pool import ConnectionPool
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.types import AuthResponse

URL = {"http": "http://localhost:8000", "websocket": "ws://localhost:8000"}
KINDS = ["http", "websocket"]


class I622SigninRejected(Exception):
    """The error the pool sign-in raises in these tests."""


def _pool(
    kind: str,
    built: list[BaseSurrealConnection],
    *,
    connect: Callable[..., Any] | None = None,
    signin: Callable[..., Any] | None = None,
    close: Callable[..., Any] | None = None,
    size: int = 1,
) -> ConnectionPool:
    """A pool with credentials whose new connections use the given fakes."""
    pool = ConnectionPool(URL[kind], "ns", "db", size=size, connection_type=kind)
    pool._credentials = ("i622_user", "i622_pass")
    original = pool._create_connection

    def create() -> BaseSurrealConnection:
        conn = original()

        async def default_connect() -> BaseSurrealConnection:
            conn._connected = True
            return conn

        async def default_signin(user: str, password: str) -> AuthResponse:
            return AuthResponse(token=f"i622-token-{len(built)}", success=True)

        async def default_close() -> None:
            conn._connected = False

        conn.connect = AsyncMock(side_effect=connect or default_connect)  # type: ignore[method-assign]
        conn.signin = AsyncMock(side_effect=signin or default_signin)  # type: ignore[method-assign]
        conn.close = AsyncMock(side_effect=close or default_close)  # type: ignore[method-assign]
        # The WebSocket release path ends the session with an ``invalidate``
        # RPC and puts the pool token back with ``authenticate``; there is no
        # transport here.
        conn.rpc = AsyncMock(return_value=None)  # type: ignore[method-assign]
        conn.authenticate = AsyncMock(return_value=AuthResponse(success=True))  # type: ignore[method-assign]
        built.append(conn)
        return conn

    pool._create_connection = create  # type: ignore[method-assign]
    return pool


def _assert_forgotten(pool: ConnectionPool, conn: BaseSurrealConnection) -> None:
    assert conn not in pool._in_use
    assert conn not in pool._pool
    assert conn not in pool._pool_auth
    assert pool._semaphore._value == pool.size


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i622_failed_signin_closes_the_connection(kind: str) -> None:
    error = I622SigninRejected("signin rejected")
    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built, signin=AsyncMock(side_effect=error))

    with pytest.raises(I622SigninRejected) as raised:
        async with pool.acquire():
            pytest.fail("acquire() must not yield a connection whose sign-in failed")

    assert raised.value is error
    assert len(built) == 1
    built[0].connect.assert_awaited_once()  # type: ignore[attr-defined]
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    _assert_forgotten(pool, built[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i622_failed_connect_closes_the_connection(kind: str) -> None:
    """A connect that fails half way (socket open, ``use`` rejected) is closed too."""
    error = OSError("i622 connect failed")
    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built, connect=AsyncMock(side_effect=error))

    with pytest.raises(OSError) as raised:
        async with pool.acquire():
            pass

    assert raised.value is error
    built[0].signin.assert_not_awaited()  # type: ignore[attr-defined]
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    _assert_forgotten(pool, built[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i622_failing_close_does_not_replace_the_init_error(kind: str) -> None:
    error = I622SigninRejected("signin rejected")
    built: list[BaseSurrealConnection] = []
    pool = _pool(
        kind,
        built,
        signin=AsyncMock(side_effect=error),
        close=AsyncMock(side_effect=RuntimeError("i622 close failed")),
    )

    with pytest.raises(I622SigninRejected) as raised:
        async with pool.acquire():
            pass

    assert raised.value is error
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    _assert_forgotten(pool, built[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i622_cancelled_init_closes_the_connection(kind: str) -> None:
    signin_started = asyncio.Event()
    never = asyncio.Event()

    async def blocked_signin(user: str, password: str) -> AuthResponse:
        signin_started.set()
        await never.wait()
        raise AssertionError("unreachable")

    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built, signin=blocked_signin)

    async def use() -> None:
        async with pool.acquire():
            pytest.fail("acquire() must not yield a connection whose sign-in was cancelled")

    task = asyncio.create_task(use())
    await asyncio.wait_for(signin_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    _assert_forgotten(pool, built[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i622_cancelled_close_still_finishes_and_frees_the_slot(kind: str) -> None:
    """Cancelled while closing the failed connection: the close completes (shielded), the slot is freed."""
    close_started = asyncio.Event()
    finish_close = asyncio.Event()
    closed = asyncio.Event()

    async def slow_close() -> None:
        close_started.set()
        await finish_close.wait()
        closed.set()

    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built, signin=AsyncMock(side_effect=I622SigninRejected("signin rejected")), close=slow_close)

    async def use() -> None:
        async with pool.acquire():
            pass

    task = asyncio.create_task(use())
    await asyncio.wait_for(close_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_forgotten(pool, built[0])
    assert not closed.is_set()
    finish_close.set()
    await asyncio.wait_for(closed.wait(), timeout=5)
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i622_pool_still_serves_after_a_failed_init(kind: str) -> None:
    attempts = 0

    async def signin_fails_once(user: str, password: str) -> AuthResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise I622SigninRejected("signin rejected")
        return AuthResponse(token="i622-token", success=True)

    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built, signin=signin_fails_once)

    with pytest.raises(I622SigninRejected):
        async with pool.acquire():
            pass

    async with pool.acquire() as conn:
        assert conn is built[1]
        assert pool.in_use == 1
        assert pool._pool_auth[conn][0] == "i622-token"
    assert pool.available == 1
    assert pool._semaphore._value == pool.size
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    built[1].close.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i622_connection_reused_from_pool_is_not_closed(kind: str) -> None:
    """A pooled connection is handed out again without init, and nothing closes it."""
    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built)

    async with pool.acquire() as first:
        pass
    async with pool.acquire() as second:
        assert second is first
        assert first.is_connected

    assert len(built) == 1
    built[0].connect.assert_awaited_once()  # type: ignore[attr-defined]
    built[0].close.assert_not_awaited()  # type: ignore[attr-defined]
    assert pool.available == 1
    assert pool._semaphore._value == pool.size

    await pool.close()
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_i622_failed_init_of_a_new_connection_leaves_reused_ones_alone() -> None:
    """With one connection in use, a second one whose sign-in fails is closed; the first is not."""
    attempts = 0

    async def second_signin_fails(user: str, password: str) -> AuthResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise I622SigninRejected("signin rejected")
        return AuthResponse(token=f"i622-token-{attempts}", success=True)

    built: list[BaseSurrealConnection] = []
    pool = _pool("http", built, signin=second_signin_fails, size=2)

    async with pool.acquire() as first:
        with pytest.raises(I622SigninRejected):
            async with pool.acquire():
                pass
        assert pool.in_use == 1
        assert first in pool._in_use
        assert pool._semaphore._value == 1

    assert len(built) == 2
    built[0].close.assert_not_awaited()  # type: ignore[attr-defined]
    built[1].close.assert_awaited_once()  # type: ignore[attr-defined]
    assert pool.available == 1
    assert pool._semaphore._value == pool.size


class _I622FakeWebSocket:
    """An open socket on which no message ever arrives."""

    def __init__(self) -> None:
        self.closed = False
        self._never = asyncio.Event()

    def __aiter__(self) -> "_I622FakeWebSocket":
        return self

    async def __anext__(self) -> Any:
        await self._never.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True
        self._never.set()


class _I622FakeSession:
    """Stands in for ``aiohttp.ClientSession``; records what it opened and whether it was closed."""

    opened: list["_I622FakeSession"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False
        self.ws = _I622FakeWebSocket()
        _I622FakeSession.opened.append(self)

    async def ws_connect(self, url: str, **kwargs: Any) -> _I622FakeWebSocket:
        return self.ws

    async def close(self) -> None:
        self.closed = True


class TestI622WebSocketConnectCancelled:
    """The real ``WebSocketConnection.connect()``: socket open, then ``use()`` fails or is cancelled."""

    @pytest.fixture(autouse=True)
    def _fake_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _I622FakeSession.opened = []
        monkeypatch.setattr(aiohttp, "ClientSession", _I622FakeSession)

    @staticmethod
    def _pool(use: Callable[..., Any]) -> tuple[ConnectionPool, list[WebSocketConnection]]:
        pool = ConnectionPool(URL["websocket"], "ns", "db", size=1, connection_type="websocket")
        built: list[WebSocketConnection] = []
        original = pool._create_connection

        def create() -> BaseSurrealConnection:
            conn = original()
            assert isinstance(conn, WebSocketConnection)
            conn.use = AsyncMock(side_effect=use)  # type: ignore[method-assign]
            built.append(conn)
            return conn

        pool._create_connection = create  # type: ignore[method-assign]
        return pool, built

    @pytest.mark.asyncio
    async def test_i622_cancelled_use_releases_session_and_reader(self) -> None:
        use_started = asyncio.Event()
        never = asyncio.Event()

        async def blocked_use(namespace: str, database: str) -> None:
            use_started.set()
            await never.wait()

        pool, built = self._pool(blocked_use)

        async def acquire() -> None:
            async with pool.acquire():
                pytest.fail("acquire() must not yield a connection whose use() was cancelled")

        task = asyncio.create_task(acquire())
        await asyncio.wait_for(use_started.wait(), timeout=5)
        reader = built[0]._reader_task
        assert reader is not None and not reader.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        (session,) = _I622FakeSession.opened
        assert session.closed
        assert session.ws.closed
        assert reader.done()
        assert not built[0].is_connected
        assert built[0]._session is None
        _assert_forgotten(pool, built[0])

    @pytest.mark.asyncio
    async def test_i622_failed_use_releases_session_and_raises_it(self) -> None:
        error = OSError("i622 use rejected")
        pool, built = self._pool(AsyncMock(side_effect=error))

        with pytest.raises(OSError) as raised:
            async with pool.acquire():
                pass

        assert raised.value is error
        (session,) = _I622FakeSession.opened
        assert session.closed
        assert session.ws.closed
        assert not built[0].is_connected
        _assert_forgotten(pool, built[0])
