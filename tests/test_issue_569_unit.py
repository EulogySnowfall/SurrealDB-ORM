"""Unit tests for #569 (v2 backport of #567): ``changes()`` streams across ``reconnect()``.

``ChangeModelStream`` resolved its HTTP client once, in ``_ensure_feed()``,
and kept it for the life of the stream. ``reconnect()`` closes that client
after the swap (and ``close_connection()`` closes it at once), so the next poll
failed with ``Not connected`` and the stream ended with ``ChangeFeedError``.
A stream that resolved its client itself now resolves it again, under the
connection name it first used, and resumes from the feed's cursor; a stream
given ``connection=`` keeps that connection and still raises.

No server is involved: the clients the connection manager builds answer
``SHOW CHANGES`` from one shared in-memory feed with the 2.7.0 semantics
(``FakeChangeFeedServer`` from the #308 tests).
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
from src.surreal_sdk.exceptions import QueryError
from src.surreal_sdk.types import QueryResponse
from surreal_sdk.exceptions import ChangeFeedError  # the class the ORM's feed raises
from tests.test_issue_308_v2_unit import FakeChangeFeedServer

_LEGACY_ATTRS = [a for a in vars(Manager) if a.startswith("_SurrealDBConnectionManager__")]


class I569Item(BaseSurrealModel):
    """A model whose table has a change feed."""

    model_config = SurrealConfigDict(table_name="i569_items")

    id: str | None = None
    v: int = 0


@dataclass
class I569Server:
    """The shared feed the HTTP clients built during a test read from."""

    feed: FakeChangeFeedServer = field(default_factory=FakeChangeFeedServer)
    built: list[Any] = field(default_factory=list)


class I569Transport(httpx.AsyncBaseTransport):
    """Never reached: the fake client answers queries itself."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "i569.jwt"}, request=request)


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
def server(monkeypatch: pytest.MonkeyPatch) -> I569Server:
    """Make the connection manager build clients served by an in-memory change feed."""
    state = I569Server()

    class I569HTTPConnection(cm.HTTPConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            state.built.append(self)

        async def connect(self) -> Any:
            self._client = httpx.AsyncClient(base_url=self.url, transport=I569Transport())
            self._connected = True
            return self

        async def signin(self, *args: Any, **kwargs: Any) -> Any:
            self._authenticated = True
            return None

        async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
            if not self._client:
                raise SDKConnectionError("Not connected. Call connect() first.")
            return await state.feed.query(sql, vars)

    monkeypatch.setattr(cm, "HTTPConnection", I569HTTPConnection)
    Manager.set_connection("http://i569.invalid", "root", "root", "i569_ns", "i569_db", protocol="json")
    return state


async def _next(stream: Any, timeout: float = 2.0) -> Any:
    return await asyncio.wait_for(anext(stream), timeout)


def _ids(events: list[Any]) -> list[str]:
    return [event.record_id for event in events]


async def test_changes_on_the_default_connection_survive_reconnect(server: I569Server) -> None:
    """A stream started before reconnect() delivers the changes written after it, without raising."""
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i569_items", {"id": "i569_items:a"})
    assert _ids([await _next(stream)]) == ["i569_items:a"]
    old = await Manager.get_client()

    new = await asyncio.wait_for(Manager.reconnect(), 1)
    assert new is not old and not old.is_connected

    server.feed.write("i569_items", {"id": "i569_items:b"})
    server.feed.write("i569_items", {"id": "i569_items:c"})
    assert _ids([await _next(stream), await _next(stream)]) == ["i569_items:b", "i569_items:c"]
    assert stream._connection is new
    stream.stop()


async def test_changes_resume_at_the_cursor_after_reconnect(server: I569Server) -> None:
    """No change is delivered twice or lost across the swap, even one written before it."""
    stream = I569Item.objects().changes(since=0, poll_interval=0.01, batch_size=2)
    for key in ("a", "b", "c"):
        server.feed.write("i569_items", {"id": f"i569_items:{key}"})
    # Two of the three entries are handed out by the first poll (batch_size=2).
    delivered = [await _next(stream), await _next(stream)]
    cursor_before = stream.cursor

    await asyncio.wait_for(Manager.reconnect(), 1)
    # Entries of another table between ours: the 2.x cursor skips them, once.
    server.feed.write("other", {"id": "other:x"})
    server.feed.write("i569_items", {"id": "i569_items:d"})
    delivered += [await _next(stream), await _next(stream)]

    assert _ids(delivered) == ["i569_items:a", "i569_items:b", "i569_items:c", "i569_items:d"]
    assert cursor_before is not None and stream.cursor is not None
    assert int(stream.cursor) > int(cursor_before)
    # Nothing else is pending: once stopped, the stream ends instead of repeating an entry.
    stream.stop()
    with pytest.raises(StopAsyncIteration):
        await _next(stream)


async def test_changes_with_an_explicit_connection_keep_it(server: I569Server) -> None:
    """With connection= passed, the stream never switches client: a closed one still fails the poll."""
    explicit = await Manager.get_client()
    stream: ChangeModelStream[I569Item] = ChangeModelStream(
        model=I569Item, connection=explicit, table="i569_items", since=0, poll_interval=0.01
    )
    server.feed.write("i569_items", {"id": "i569_items:a"})
    assert _ids([await _next(stream)]) == ["i569_items:a"]

    await asyncio.wait_for(Manager.reconnect(), 1)
    server.feed.write("i569_items", {"id": "i569_items:b"})

    with pytest.raises(ChangeFeedError, match="Not connected"):
        await _next(stream)
    assert stream._connection is explicit


async def test_a_statement_error_still_raises_and_does_not_loop(server: I569Server) -> None:
    """A rejected poll on a connected client is not a reconnect: it raises once, no retry."""
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i569_items", {"id": "i569_items:a"})
    await _next(stream)
    built = len(server.built)
    server.feed.statement_error = "The table 'i569_items' does not exist"

    with pytest.raises(ChangeFeedError, match="does not exist"):
        await _next(stream)
    assert len(server.built) == built, "a statement error made the stream build a new client"


async def test_a_failed_request_on_a_connected_client_still_raises(server: I569Server) -> None:
    """A request failure while the client is still connected raises as before."""
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i569_items", {"id": "i569_items:a"})
    await _next(stream)
    server.feed.fail_with = QueryError("Request failed: server unreachable")

    with pytest.raises(ChangeFeedError, match="server unreachable"):
        await _next(stream)


async def test_changes_stopped_before_the_swap_are_not_restarted(server: I569Server) -> None:
    """stop() ends the stream; a reconnect() afterwards does not bring it back."""
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i569_items", {"id": "i569_items:a"})
    await _next(stream)
    stream.stop()
    await asyncio.wait_for(Manager.reconnect(), 1)
    server.feed.write("i569_items", {"id": "i569_items:b"})

    with pytest.raises(StopAsyncIteration):
        await _next(stream)


async def test_changes_stopped_during_the_client_switch_end(server: I569Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """stop() while the new client is being resolved ends the stream; no new feed starts."""
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i569_items", {"id": "i569_items:a"})
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
    server.feed.write("i569_items", {"id": "i569_items:b"})
    pending = asyncio.ensure_future(_next(stream))
    await asyncio.wait_for(entered.wait(), 1)
    stream.stop()
    gate.set()

    with pytest.raises(StopAsyncIteration):
        await pending
    assert stream._connection is old, "a stopped stream moved to a new client"


async def test_changes_stopped_while_a_failing_poll_is_in_flight_end(server: I569Server) -> None:
    """A poll that fails after stop() ends the stream instead of raising ChangeFeedError."""
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i569_items", {"id": "i569_items:a"})
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


async def test_changes_resume_on_the_connection_first_resolved(server: I569Server) -> None:
    """A stream first read under using("a") resumes on "a", even when iterated outside using()."""
    Manager.add_connection(
        "a",
        url="http://i569-a.invalid",
        user="root",
        password="root",
        namespace="i569_ns",
        database="i569_a",
        protocol="json",
    )
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i569_items", {"id": "i569_items:a"})
    async with Manager.using("a"):
        assert _ids([await _next(stream)]) == ["i569_items:a"]
    first = Manager._clients["a"]
    assert stream._connection is first

    await Manager.close_connection("a")
    server.feed.write("i569_items", {"id": "i569_items:b"})
    assert _ids([await _next(stream)]) == ["i569_items:b"]

    assert stream._connection is not first
    assert stream._connection is Manager._clients["a"]
    assert stream._connection.database == "i569_a"
    stream.stop()
