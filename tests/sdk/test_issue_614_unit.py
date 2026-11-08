"""Issue #614 — ``ConnectionPool.acquire()`` closes a connection whose initialisation fails.

``acquire()`` builds a new connection and initialises it (``connect()``, then
the pool sign-in). When that raised, the semaphore slot was released but the
connection, already opened by ``connect()``, was left open: nothing referenced
it any more and nothing closed it.

The connections here are real ``HTTPConnection`` / ``WebSocketConnection``
objects whose ``connect()``, ``signin()`` and ``close()`` are replaced by
mocks, so no server is needed.
"""

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.surreal_sdk.connection.base import BaseSurrealConnection
from src.surreal_sdk.connection.pool import ConnectionPool
from src.surreal_sdk.types import AuthResponse

URL = {"http": "http://localhost:8000", "websocket": "ws://localhost:8000"}
KINDS = ["http", "websocket"]


class I614SigninRejected(Exception):
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
    pool._credentials = ("i614_user", "i614_pass")
    original = pool._create_connection

    def create() -> BaseSurrealConnection:
        conn = original()

        async def default_connect() -> BaseSurrealConnection:
            conn._connected = True
            return conn

        async def default_signin(user: str, password: str) -> AuthResponse:
            return AuthResponse(token=f"i614-token-{len(built)}", success=True)

        async def default_close() -> None:
            conn._connected = False

        conn.connect = AsyncMock(side_effect=connect or default_connect)  # type: ignore[method-assign]
        conn.signin = AsyncMock(side_effect=signin or default_signin)  # type: ignore[method-assign]
        conn.close = AsyncMock(side_effect=close or default_close)  # type: ignore[method-assign]
        # The WebSocket release path puts the pool token back with an
        # ``authenticate`` RPC; there is no transport here.
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
async def test_i614_failed_signin_closes_the_connection(kind: str) -> None:
    error = I614SigninRejected("signin rejected")
    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built, signin=AsyncMock(side_effect=error))

    with pytest.raises(I614SigninRejected) as raised:
        async with pool.acquire():
            pytest.fail("acquire() must not yield a connection whose sign-in failed")

    assert raised.value is error
    assert len(built) == 1
    built[0].connect.assert_awaited_once()  # type: ignore[attr-defined]
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    _assert_forgotten(pool, built[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i614_failed_connect_closes_the_connection(kind: str) -> None:
    """A connect that fails half way (socket open, ``use`` rejected) is closed too."""
    error = OSError("i614 connect failed")
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
async def test_i614_failing_close_does_not_replace_the_init_error(kind: str) -> None:
    error = I614SigninRejected("signin rejected")
    built: list[BaseSurrealConnection] = []
    pool = _pool(
        kind,
        built,
        signin=AsyncMock(side_effect=error),
        close=AsyncMock(side_effect=RuntimeError("i614 close failed")),
    )

    with pytest.raises(I614SigninRejected) as raised:
        async with pool.acquire():
            pass

    assert raised.value is error
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    _assert_forgotten(pool, built[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i614_cancelled_init_closes_the_connection(kind: str) -> None:
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
async def test_i614_cancelled_close_still_finishes_and_frees_the_slot(kind: str) -> None:
    """Cancelled while closing the failed connection: the close completes (shielded), the slot is freed."""
    close_started = asyncio.Event()
    finish_close = asyncio.Event()
    closed = asyncio.Event()

    async def slow_close() -> None:
        close_started.set()
        await finish_close.wait()
        closed.set()

    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built, signin=AsyncMock(side_effect=I614SigninRejected("signin rejected")), close=slow_close)

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
async def test_i614_pool_still_serves_after_a_failed_init(kind: str) -> None:
    attempts = 0

    async def signin_fails_once(user: str, password: str) -> AuthResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise I614SigninRejected("signin rejected")
        return AuthResponse(token="i614-token", success=True)

    built: list[BaseSurrealConnection] = []
    pool = _pool(kind, built, signin=signin_fails_once)

    with pytest.raises(I614SigninRejected):
        async with pool.acquire():
            pass

    async with pool.acquire() as conn:
        assert conn is built[1]
        assert pool.in_use == 1
        assert pool._pool_auth[conn][0] == "i614-token"
    assert pool.available == 1
    assert pool._semaphore._value == pool.size
    built[0].close.assert_awaited_once()  # type: ignore[attr-defined]
    built[1].close.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_i614_connection_reused_from_pool_is_not_closed(kind: str) -> None:
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
