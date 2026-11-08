"""Unit tests for #270: follow-ups to the non-disruptive ``reconnect()`` (#232).

1. ``QuerySet.changes()`` resolved its HTTP client once and kept it. After a
   ``reconnect()`` that client is closed, so the next poll failed with
   ``Not connected`` and the stream ended with ``ChangeFeedError``. A stream
   that resolved its client itself now resolves it again and resumes from its
   cursor; one given ``connection=`` keeps using that connection.
2. ``_build_client()`` closed a half-built client on ``Exception`` only, and
   ``asyncio.CancelledError`` is not one: cancelling ``reconnect()`` or
   ``get_client()`` during ``connect()`` or ``signin()`` left that client open
   with nothing referring to it.

No server is involved: the clients the connection manager builds answer
``SHOW CHANGES`` from one shared in-memory feed (``FakeChangeFeedServer``),
and ``connect()``/``signin()`` can be held open.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm import SurrealDBConnectionManager as Manager
from src.surreal_orm import connection_manager as cm
from src.surreal_orm.live import ChangeModelStream
from src.surreal_sdk.exceptions import ConnectionError as SDKConnectionError
from src.surreal_sdk.types import QueryResponse
from tests.sdk.test_streaming import FakeChangeFeedServer

_LEGACY_ATTRS = [a for a in vars(Manager) if a.startswith("_SurrealDBConnectionManager__")]


class I270Item(BaseSurrealModel):
    """A model whose table has a change feed."""

    model_config = SurrealConfigDict(table_name="i270_items")

    id: str | None = None
    v: int = 0


@dataclass
class I270Server:
    """Controls the HTTP clients the connection manager builds during a test."""

    feed: FakeChangeFeedServer = field(default_factory=FakeChangeFeedServer)
    built: list[Any] = field(default_factory=list)
    #: Stage at which the next client blocks: ``"connect"``, ``"signin"`` or ``None``.
    block_at: str | None = None
    blocked: asyncio.Event = field(default_factory=asyncio.Event)
    transports: list[I270Transport] = field(default_factory=list)


class I270Transport(httpx.AsyncBaseTransport):
    """Records whether the ``httpx`` client of a half-built connection was closed."""

    def __init__(self) -> None:
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "i270.jwt"}, request=request)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def isolated_registry() -> Iterator[None]:
    """Run each test against an empty registry, and restore the real one afterwards."""
    saved_configs = dict(Manager._configs)
    saved_clients = dict(Manager._clients)
    saved_ws = dict(Manager._ws_clients)
    saved_lock = Manager._connection_lock
    saved_ws_lock = Manager._ws_connection_lock
    saved_legacy = {a: getattr(Manager, a) for a in _LEGACY_ATTRS}
    Manager._configs.clear()
    Manager._clients.clear()
    Manager._ws_clients.clear()
    Manager._connection_lock = None
    Manager._ws_connection_lock = None
    try:
        yield
    finally:
        Manager._configs.clear()
        Manager._configs.update(saved_configs)
        Manager._clients.clear()
        Manager._clients.update(saved_clients)
        Manager._ws_clients.clear()
        Manager._ws_clients.update(saved_ws)
        Manager._connection_lock = saved_lock
        Manager._ws_connection_lock = saved_ws_lock
        for attr, value in saved_legacy.items():
            setattr(Manager, attr, value)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> I270Server:
    """Make the connection manager build clients served by an in-memory change feed."""
    state = I270Server()

    class I270HTTPConnection(cm.HTTPConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            state.built.append(self)

        async def connect(self) -> Any:
            transport = I270Transport()
            state.transports.append(transport)
            self._client = httpx.AsyncClient(base_url=self.url, transport=transport)
            self._connected = True
            if state.block_at == "connect":
                state.blocked.set()
                await asyncio.Event().wait()
            return self

        async def signin(self, *args: Any, **kwargs: Any) -> Any:
            if state.block_at == "signin":
                state.blocked.set()
                await asyncio.Event().wait()
            self._authenticated = True
            return None

        async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
            if not self._client:
                raise SDKConnectionError("Not connected. Call connect() first.")
            if "SHOW CHANGES" not in sql:
                return QueryResponse.from_rpc_result([{"status": "OK", "result": None}])
            return await state.feed.query(sql, vars)

    monkeypatch.setattr(cm, "HTTPConnection", I270HTTPConnection)
    Manager.set_connection("http://i270.invalid", "root", "root", "i270_ns", "i270_db", protocol="json")
    return state


async def _next(stream: Any, timeout: float = 2.0) -> Any:
    return await asyncio.wait_for(anext(stream), timeout)


def _ids(events: list[Any]) -> list[str]:
    return [event.record_id for event in events]


# ==================== changes() across reconnect() ====================


async def test_changes_on_the_default_connection_survive_reconnect(server: I270Server) -> None:
    """A stream started before reconnect() delivers the changes written after it, without raising."""
    stream = I270Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i270_items", {"id": "i270_items:a"})
    assert _ids([await _next(stream)]) == ["i270_items:a"]
    old = await Manager.get_client()

    new = await asyncio.wait_for(Manager.reconnect(), 1)
    assert new is not old and not old.is_connected

    server.feed.write("i270_items", {"id": "i270_items:b"})
    server.feed.write("i270_items", {"id": "i270_items:c"})
    assert _ids([await _next(stream), await _next(stream)]) == ["i270_items:b", "i270_items:c"]
    assert stream._connection is new
    stream.stop()


async def test_changes_resume_at_the_cursor_after_reconnect(server: I270Server) -> None:
    """No change is delivered twice or lost across the client swap, even when written before it."""
    stream = I270Item.objects().changes(since=0, poll_interval=0.01, batch_size=2)
    for key in ("a", "b", "c"):
        server.feed.write("i270_items", {"id": f"i270_items:{key}"})
    # Two of the three entries are handed out by the first poll (batch_size=2).
    delivered = [await _next(stream), await _next(stream)]
    cursor_before = stream.cursor

    await asyncio.wait_for(Manager.reconnect(), 1)
    server.feed.write("i270_items", {"id": "i270_items:d"})
    delivered += [await _next(stream), await _next(stream)]

    assert _ids(delivered) == ["i270_items:a", "i270_items:b", "i270_items:c", "i270_items:d"]
    assert cursor_before is not None and stream.cursor is not None
    assert int(stream.cursor) > int(cursor_before)
    # Nothing else is pending: once stopped, the stream ends instead of repeating an entry.
    stream.stop()
    with pytest.raises(StopAsyncIteration):
        await _next(stream)


async def test_changes_with_an_explicit_connection_keep_it(server: I270Server) -> None:
    """With connection= passed, the stream never switches client: a closed one still fails the poll."""
    explicit = await Manager.get_client()
    stream: ChangeModelStream[I270Item] = ChangeModelStream(
        model=I270Item, connection=explicit, table="i270_items", since=0, poll_interval=0.01
    )
    server.feed.write("i270_items", {"id": "i270_items:a"})
    assert _ids([await _next(stream)]) == ["i270_items:a"]

    await asyncio.wait_for(Manager.reconnect(), 1)
    server.feed.write("i270_items", {"id": "i270_items:b"})

    with pytest.raises(cm.SurrealDBError, match="Not connected"):
        await _next(stream)
    assert stream._connection is explicit


async def test_changes_stopped_before_the_swap_are_not_restarted(server: I270Server) -> None:
    """stop() ends the stream; a reconnect() afterwards does not bring it back."""
    stream = I270Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i270_items", {"id": "i270_items:a"})
    await _next(stream)
    stream.stop()
    await asyncio.wait_for(Manager.reconnect(), 1)
    server.feed.write("i270_items", {"id": "i270_items:b"})

    with pytest.raises(StopAsyncIteration):
        await _next(stream)


async def test_changes_stopped_during_the_replacement_end(server: I270Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() while the replacement client is being resolved ends the stream; no new feed starts."""
    stream = I270Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i270_items", {"id": "i270_items:a"})
    await _next(stream)
    old = await Manager.get_client()
    await Manager.close_connection()

    real_get_client = Manager.get_client
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def held_get_client(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await gate.wait()
        return await real_get_client(*args, **kwargs)

    monkeypatch.setattr(Manager, "get_client", held_get_client)
    server.feed.write("i270_items", {"id": "i270_items:b"})
    pending = asyncio.ensure_future(_next(stream))
    await asyncio.wait_for(entered.wait(), 1)
    stream.stop()
    gate.set()

    with pytest.raises(StopAsyncIteration):
        await pending
    assert stream._connection is old, "a stopped stream moved to a new client"


async def test_changes_stopped_while_a_failing_poll_is_in_flight_end(server: I270Server) -> None:
    """A poll that fails after stop() ends the stream instead of raising ChangeFeedError."""
    stream = I270Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i270_items", {"id": "i270_items:a"})
    await _next(stream)

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def failing_query(sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        entered.set()
        await gate.wait()
        raise SDKConnectionError("Not connected. Call connect() first.")

    server.feed.query = failing_query  # type: ignore[method-assign]
    pending = asyncio.ensure_future(_next(stream))
    await asyncio.wait_for(entered.wait(), 1)
    stream.stop()
    await Manager.close_connection()
    gate.set()

    with pytest.raises(StopAsyncIteration):
        await pending


async def test_changes_resume_on_the_connection_first_resolved(server: I270Server) -> None:
    """A stream first read under using("a") resumes on "a", even when iterated outside using()."""
    Manager.add_connection(
        "a",
        url="http://i270-a.invalid",
        user="root",
        password="root",
        namespace="i270_ns",
        database="i270_a",
        protocol="json",
    )
    stream = I270Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i270_items", {"id": "i270_items:a"})
    async with Manager.using("a"):
        assert _ids([await _next(stream)]) == ["i270_items:a"]
    first = Manager._clients["a"]
    assert stream._connection is first

    await Manager.close_connection("a")
    server.feed.write("i270_items", {"id": "i270_items:b"})
    assert _ids([await _next(stream)]) == ["i270_items:b"]

    assert stream._connection is not first
    assert stream._connection is Manager._clients["a"]
    assert stream._connection.database == "i270_a"
    stream.stop()


# ==================== a cancelled build closes its client ====================


@pytest.mark.parametrize("stage", ["connect", "signin"])
async def test_cancelled_reconnect_closes_the_half_built_client(server: I270Server, stage: str) -> None:
    """wait_for(reconnect()) expiring during the build closes the new client and keeps the previous one."""
    previous = await Manager.get_client()
    server.block_at = stage

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(Manager.reconnect(), 0.1)

    half_built = server.built[-1]
    assert half_built is not previous
    assert server.blocked.is_set()
    assert not half_built.is_connected, "the half-built client was left open"
    assert server.transports[-1].closed, "the half-built client's transport was never closed"
    assert Manager._clients == {"default": previous}
    assert previous.is_connected


@pytest.mark.parametrize("stage", ["connect", "signin"])
async def test_cancelled_get_client_closes_the_half_built_client(server: I270Server, stage: str) -> None:
    """Cancelling get_client() during the build re-raises CancelledError and leaves the registry as it was."""
    previous = await Manager.get_client()
    await previous.close()  # get_client() now builds a new client
    server.block_at = stage

    task = asyncio.create_task(Manager.get_client())
    await asyncio.wait_for(server.blocked.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    half_built = server.built[-1]
    assert half_built is not previous
    assert not half_built.is_connected, "the half-built client was left open"
    assert server.transports[-1].closed, "the half-built client's transport was never closed"
    assert Manager._clients == {"default": previous}
