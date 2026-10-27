"""Unit tests for #232: non-disruptive ``reconnect()`` and a close that cannot leave a dead client behind.

``reconnect()`` used to close the shared HTTP client first and build the new
one afterwards, so every request still waiting on the old client failed with
``ConnectionError: Request failed``. It now builds and authenticates the new
client first, swaps it in, and closes the old one once no request is in flight
on it (bounded by a timeout). An open HTTP transaction counts as using the
client too: it sends its statements only at commit, so closing the old client
under it made the commit fail and lost the whole transaction.

``_close_single()`` only caught ``NotImplementedError`` around the HTTP close,
so ``RuntimeError: Event loop is closed`` escaped ``close_connection()`` before
the registry was cleared, and ``get_client()`` kept handing back the dead
client.

No server is involved: each HTTP client gets an in-memory ``httpx`` transport
whose ``/rpc`` requests can be held open, and whose close fails the requests
still waiting, as closing a real connection pool does.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from src.surreal_orm import SurrealDBConnectionManager as Manager
from src.surreal_orm import connection_manager as cm
from src.surreal_orm.connection_manager import SurrealDbConnectionError

#: A query containing this marker is held open until its transport releases it.
HOLD = "RETURN 'I232 hold';"

_LEGACY_ATTRS = [a for a in vars(Manager) if a.startswith("_SurrealDBConnectionManager__")]


class I232Transport(httpx.AsyncBaseTransport):
    """In-memory SurrealDB HTTP endpoint for one client."""

    def __init__(self, *, fail_signin: bool = False, close_error: BaseException | None = None) -> None:
        self.fail_signin = fail_signin
        self.close_error = close_error
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.queries: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/signin":
            if self.fail_signin:
                return httpx.Response(401, text="There was a problem with authentication", request=request)
            return httpx.Response(200, json={"token": "i232.jwt"}, request=request)

        await request.aread()
        rpc = json.loads(request.content)
        sql = rpc["params"][0] if rpc["params"] else ""
        self.queries.append(sql)
        if sql == HOLD:
            self.held.set()
            await self._wait_for_release_or_close()
            if not self.release.is_set():
                raise httpx.ReadError("connection closed while reading the response", request=request)
        result = [{"status": "OK", "result": sql, "time": "1µs"}]
        return httpx.Response(200, json={"id": rpc["id"], "result": result}, request=request)

    async def _wait_for_release_or_close(self) -> None:
        waiters = [asyncio.ensure_future(self.release.wait()), asyncio.ensure_future(self.closed.wait())]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    async def aclose(self) -> None:
        self.closed.set()
        if self.close_error is not None:
            raise self.close_error


@dataclass
class I232Server:
    """Controls the HTTP clients the connection manager builds during a test."""

    built: list[Any] = field(default_factory=list)
    fail_signin: bool = False
    fail_connect: bool = False
    close_error: BaseException | None = None


def _transport(client: Any) -> I232Transport:
    transport = client.i232_transport
    assert isinstance(transport, I232Transport)
    return transport


@pytest.fixture(autouse=True)
def isolated_registry() -> Iterator[None]:
    """Run each test against an empty registry, and restore the real one afterwards."""
    saved_configs = dict(Manager._configs)
    saved_clients = dict(Manager._clients)
    saved_ws = dict(Manager._ws_clients)
    saved_lock = Manager._connection_lock
    saved_legacy = {a: getattr(Manager, a) for a in _LEGACY_ATTRS}
    Manager._configs.clear()
    Manager._clients.clear()
    Manager._ws_clients.clear()
    Manager._connection_lock = None
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
        for attr, value in saved_legacy.items():
            setattr(Manager, attr, value)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> I232Server:
    """Make the connection manager build HTTP clients on an in-memory transport."""
    state = I232Server()

    class I232HTTPConnection(cm.HTTPConnection):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.i232_transport = I232Transport(fail_signin=state.fail_signin, close_error=state.close_error)
            state.built.append(self)

        async def connect(self) -> Any:
            if state.fail_connect:
                raise OSError("connection refused")
            self._client = httpx.AsyncClient(base_url=self.url, transport=self.i232_transport)
            self._connected = True
            return self

    monkeypatch.setattr(cm, "HTTPConnection", I232HTTPConnection)
    Manager.set_connection("http://i232.invalid", "root", "root", "i232_ns", "i232_db", protocol="json")
    return state


async def _wait_for_swap(old: Any) -> Any:
    """Return the default client once it is no longer *old*."""
    for _ in range(1000):
        current = Manager._clients.get("default")
        if current is not None and current is not old:
            return current
        await asyncio.sleep(0.001)
    pytest.fail("reconnect() never installed a new default client")


def _other_tasks() -> set[asyncio.Task[Any]]:
    return {t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()}


# ==================== reconnect() ====================


async def test_reconnect_lets_an_in_flight_request_finish(server: I232Server) -> None:
    """The new client is installed at once; the old one closes only after its request completes."""
    old = await Manager.get_client()
    in_flight = asyncio.create_task(old.query(HOLD))
    await asyncio.wait_for(_transport(old).held.wait(), 1)

    swap = asyncio.create_task(Manager.reconnect())
    new = await _wait_for_swap(old)

    assert new.is_connected
    assert Manager._SurrealDBConnectionManager__client is new  # type: ignore[attr-defined]
    assert await Manager.get_client() is new
    assert old.is_connected, "the old client was closed while a request was still in flight on it"
    assert not swap.done(), "reconnect() returned before the old client's request finished"

    _transport(old).release.set()
    response = await asyncio.wait_for(in_flight, 1)
    assert response.is_ok
    assert response.first_result is not None and response.first_result.result == HOLD

    assert await asyncio.wait_for(swap, 1) is new
    assert not old.is_connected
    assert _transport(old).closed.is_set()


async def test_reconnect_without_in_flight_request_closes_old_client(server: I232Server) -> None:
    """With nothing in flight, the old client is closed before reconnect() returns."""
    old = await Manager.get_client()

    new = await asyncio.wait_for(Manager.reconnect(), 1)

    assert new is not old and new is not None
    assert new.is_connected
    assert not old.is_connected
    assert Manager._clients == {"default": new}


async def test_reconnect_drain_is_bounded(
    server: I232Server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A request that never completes does not hold reconnect(): the old client closes after the timeout."""
    monkeypatch.setattr(Manager, "_reconnect_drain_timeout", 0.05, raising=False)
    old = await Manager.get_client()
    stuck = asyncio.create_task(old.query(HOLD))
    await asyncio.wait_for(_transport(old).held.wait(), 1)
    before = _other_tasks() - {stuck}

    with caplog.at_level(logging.WARNING, logger=cm.__name__):
        new = await asyncio.wait_for(Manager.reconnect(), 2)

    assert new is not old and new is not None and new.is_connected
    assert not old.is_connected
    # The close cuts off the stuck request; nothing else is left running.
    with pytest.raises(cm.SurrealDBError):
        await asyncio.wait_for(stuck, 1)
    await asyncio.sleep(0)
    assert _other_tasks() - before == set()
    assert any("still in flight" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("failure", ["signin", "connect"])
async def test_reconnect_failure_leaves_the_registry_unchanged(server: I232Server, failure: str) -> None:
    """If the new client cannot connect or authenticate, the current one stays in place."""
    old = await Manager.get_client()
    ws_sentinel = AsyncMock()
    Manager._ws_clients["default"] = ws_sentinel
    clients_before = dict(Manager._clients)
    ws_before = dict(Manager._ws_clients)
    if failure == "signin":
        server.fail_signin = True
    else:
        server.fail_connect = True

    with pytest.raises(SurrealDbConnectionError):
        await Manager.reconnect()

    assert Manager._clients == clients_before
    assert Manager._ws_clients == ws_before
    assert Manager._SurrealDBConnectionManager__client is old  # type: ignore[attr-defined]
    assert old.is_connected
    assert await Manager.get_client() is old
    # The half-built client was closed, not leaked.
    assert server.built[-1] is not old
    assert not server.built[-1].is_connected
    ws_sentinel.close.assert_not_awaited()
    Manager._ws_clients.clear()


async def test_reconnect_when_not_configured_raises_value_error() -> None:
    """Without a default configuration, reconnect() still reports it as before."""
    with pytest.raises(ValueError, match="not configured"):
        await Manager.reconnect()


# ==================== closing a dead client ====================


@pytest.mark.parametrize("target", ["all", "default", "named", "remove"])
async def test_close_with_failing_http_close_clears_the_registry(
    server: I232Server, caplog: pytest.LogCaptureFixture, target: str
) -> None:
    """A client whose close raises is logged and unregistered; get_client() builds a new one."""
    server.close_error = RuntimeError("Event loop is closed")
    name = "default"
    if target in ("named", "remove"):
        name = "i232_other"
        Manager.add_connection(
            name,
            url="http://i232.invalid",
            user="root",
            password="root",
            namespace="i232_ns",
            database="i232_db",
            protocol="json",
        )
    dead = await Manager.get_client(name)
    server.close_error = None  # the replacement client closes normally

    with caplog.at_level(logging.WARNING, logger=cm.__name__):
        if target == "all":
            await Manager.close_connection()
        elif target == "remove":
            await Manager.remove_connection(name)
        else:
            await Manager.close_connection(name)

    assert name not in Manager._clients
    assert not dead.is_connected
    if name == "default":
        assert Manager._SurrealDBConnectionManager__client is None  # type: ignore[attr-defined]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.exc_info is not None]
    assert warnings, "the failed close was not logged with its traceback"
    assert isinstance(warnings[0].exc_info[1], RuntimeError)  # type: ignore[index]

    if target != "remove":
        fresh = await Manager.get_client(name)
        assert fresh is not dead
        assert fresh.is_connected


async def test_http_close_marks_connection_closed_even_when_it_raises(server: I232Server) -> None:
    """``HTTPConnection.close()`` resets its state before the underlying close can fail."""
    server.close_error = RuntimeError("Event loop is closed")
    client = await Manager.get_client()

    with pytest.raises(RuntimeError, match="Event loop is closed"):
        await client.close()

    assert not client.is_connected
    assert not client.is_authenticated
    assert client._client is None


# ==================== HTTPConnection drain ====================


async def test_drain_and_close_waits_for_back_to_back_requests(server: I232Server) -> None:
    """The drain re-checks the in-flight count, so a request started right after another is waited for too."""
    client = await Manager.get_client()
    transport = _transport(client)
    first = asyncio.create_task(client.query(HOLD))
    await asyncio.wait_for(transport.held.wait(), 1)
    drain = asyncio.create_task(client._drain_and_close(1))
    await asyncio.sleep(0.01)
    assert not drain.done()

    second = asyncio.create_task(client.query("RETURN 2;"))
    transport.release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)

    assert await asyncio.wait_for(drain, 1) is True
    assert not client.is_connected
    assert client._inflight_rpcs == 0


async def test_in_flight_count_is_released_when_a_request_fails(server: I232Server) -> None:
    """A failing request does not leave the client counted as busy."""
    client = await Manager.get_client()
    transport = _transport(client)
    request = asyncio.create_task(client.query(HOLD))
    await asyncio.wait_for(transport.held.wait(), 1)
    assert client._inflight_rpcs == 1

    await transport.aclose()  # fails the held request
    with pytest.raises(cm.SurrealDBError):
        await asyncio.wait_for(request, 1)

    assert client._inflight_rpcs == 0
    assert await asyncio.wait_for(client._drain_and_close(1), 1) is True


# ==================== open transactions ====================


def _committed_sql(client: Any) -> list[str]:
    """The batched transaction queries *client* sent."""
    return [q for q in _transport(client).queries if q.startswith("BEGIN TRANSACTION;")]


async def test_reconnect_waits_for_a_transaction_open_on_the_old_client(server: I232Server) -> None:
    """A transaction open across the swap commits on the old client, which is closed only afterwards."""
    old = await Manager.get_client()
    opened = asyncio.Event()
    finish = asyncio.Event()

    async def body() -> Any:
        async with await Manager.transaction() as tx:
            await tx.query("CREATE i232_item:a;")
            opened.set()
            await finish.wait()
            await tx.query("CREATE i232_item:b;")
        return tx

    work = asyncio.create_task(body())
    await asyncio.wait_for(opened.wait(), 1)
    swap = asyncio.create_task(Manager.reconnect())
    new = await _wait_for_swap(old)
    await asyncio.sleep(0.01)

    assert old.is_connected, "the old client was closed under an open transaction"
    assert not swap.done(), "reconnect() returned before the open transaction ended"

    finish.set()
    tx = await asyncio.wait_for(work, 1)
    assert tx.is_committed
    assert await asyncio.wait_for(swap, 1) is new
    assert not old.is_connected
    [sql] = _committed_sql(old)
    assert "CREATE i232_item:a;" in sql and "CREATE i232_item:b;" in sql
    assert _committed_sql(new) == []


async def test_reconnect_inside_a_transaction_does_not_wait_for_it(server: I232Server) -> None:
    """reconnect() called from the body of a transaction returns at once; the old client closes after the commit."""
    old = await Manager.get_client()

    async with await Manager.transaction() as tx:
        await tx.query("CREATE i232_item:a;")
        # Waiting for this transaction would wait for this very task: the
        # default 30 s drain would expire and the commit below would fail.
        new = await asyncio.wait_for(Manager.reconnect(), 1)
        assert new is not old and new is not None and new.is_connected
        assert await Manager.get_client() is new
        assert old.is_connected
        await tx.query("CREATE i232_item:b;")

    assert tx.is_committed
    assert not old.is_connected
    [sql] = _committed_sql(old)
    assert "CREATE i232_item:b;" in sql


async def test_reconnect_drain_is_bounded_for_a_transaction_that_never_ends(
    server: I232Server, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A transaction left open does not hold reconnect() past the timeout, and leaves no task behind."""
    monkeypatch.setattr(Manager, "_reconnect_drain_timeout", 0.05, raising=False)
    old = await Manager.get_client()
    opened = asyncio.Event()

    async def abandoned() -> None:
        async with await Manager.transaction() as tx:
            await tx.query("CREATE i232_item:a;")
            opened.set()
            await asyncio.Event().wait()  # never finishes

    holder = asyncio.create_task(abandoned())
    await asyncio.wait_for(opened.wait(), 1)
    before = _other_tasks() - {holder}

    with caplog.at_level(logging.WARNING, logger=cm.__name__):
        new = await asyncio.wait_for(Manager.reconnect(), 2)

    assert new is not old and new is not None and new.is_connected
    assert not old.is_connected
    assert any("still in flight" in r.getMessage() for r in caplog.records)
    await asyncio.sleep(0)
    assert _other_tasks() - before == {holder}

    holder.cancel()  # the transaction rolls back on its (closed) client without error
    with pytest.raises(asyncio.CancelledError):
        await holder
    assert old._open_transactions == {}


@pytest.mark.parametrize("ending", ["commit", "failed commit", "rollback"])
async def test_an_ended_transaction_no_longer_holds_the_client(
    server: I232Server, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    """However a transaction ends, the client is no longer counted as in use by it."""
    client = await Manager.get_client()
    if ending == "failed commit":
        monkeypatch.setattr(client, "query", AsyncMock(side_effect=RuntimeError("commit refused")))

    expected = contextlib.nullcontext() if ending == "commit" else pytest.raises((RuntimeError, cm.SurrealDBError))
    with expected:
        async with client.transaction() as tx:
            await tx.query("CREATE i232_item:a;")
            assert client._open_transactions == {asyncio.current_task(): 1}
            if ending == "rollback":
                raise RuntimeError("abort")

    assert tx.is_committed is (ending == "commit")
    assert client._open_transactions == {}
    assert await asyncio.wait_for(client._drain_and_close(1), 0.5) is True
    assert not client.is_connected


# ==================== cancelled reconnect() ====================


async def test_cancelled_reconnect_closes_the_old_client_only_after_its_request(server: I232Server) -> None:
    """Cancelling reconnect() during the drain keeps the old client open until its request finishes."""
    old = await Manager.get_client()
    in_flight = asyncio.create_task(old.query(HOLD))
    await asyncio.wait_for(_transport(old).held.wait(), 1)
    before = _other_tasks() - {in_flight}

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(Manager.reconnect(), 0.1)

    new = Manager._clients["default"]
    assert new is not old and new.is_connected
    assert Manager._SurrealDBConnectionManager__client is new  # type: ignore[attr-defined]
    assert old.is_connected, "the cancelled reconnect() closed the old client under its request"

    _transport(old).release.set()
    response = await asyncio.wait_for(in_flight, 1)
    assert response.is_ok
    assert not old.is_connected, "the old client was not closed once its last request finished"
    assert _transport(old).closed.is_set()
    await asyncio.sleep(0)
    assert _other_tasks() - before == set()


async def test_cancelled_reconnect_closes_the_old_client_only_after_its_transaction(server: I232Server) -> None:
    """Cancelling reconnect() during the drain keeps the old client open until an open transaction commits."""
    old = await Manager.get_client()
    opened = asyncio.Event()
    finish = asyncio.Event()

    async def body() -> Any:
        async with await Manager.transaction() as tx:
            await tx.query("CREATE i232_item:a;")
            opened.set()
            await finish.wait()
        return tx

    work = asyncio.create_task(body())
    await asyncio.wait_for(opened.wait(), 1)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(Manager.reconnect(), 0.1)

    assert Manager._clients["default"] is not old
    assert old.is_connected, "the cancelled reconnect() closed the old client under an open transaction"

    finish.set()
    tx = await asyncio.wait_for(work, 1)
    assert tx.is_committed
    assert not old.is_connected
    [sql] = _committed_sql(old)
    assert "CREATE i232_item:a;" in sql


async def test_reconnect_cancelled_while_closing_the_websocket_still_retires_the_old_client(server: I232Server) -> None:
    """A cancellation before the drain starts does not leave the old HTTP client open for good."""
    old = await Manager.get_client()
    in_flight = asyncio.create_task(old.query(HOLD))
    await asyncio.wait_for(_transport(old).held.wait(), 1)

    async def close_forever() -> None:
        await asyncio.Event().wait()

    ws = AsyncMock()
    ws.close.side_effect = close_forever
    Manager._ws_clients["default"] = ws

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(Manager.reconnect(), 0.1)

    ws.close.assert_awaited_once()
    assert "default" not in Manager._ws_clients
    assert old.is_connected

    _transport(old).release.set()
    assert (await asyncio.wait_for(in_flight, 1)).is_ok
    assert not old.is_connected, "the old client stayed open after the cancelled reconnect()"


async def test_cancelled_drain_closes_at_once_when_nothing_is_left(server: I232Server) -> None:
    """A drain cancelled after its last request ended, but before it resumed, closes the client itself."""
    client = await Manager.get_client()
    client._inflight_rpcs = 1  # a request in flight, ended by hand below
    drain = asyncio.create_task(client._drain_and_close(1))
    await asyncio.sleep(0.01)
    assert not drain.done()

    # The request ends and the drain is cancelled in the same step, so the
    # cancellation reaches the drain with nothing left using the client.
    client._inflight_rpcs = 0
    client._released.set()
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain

    assert not client.is_connected
    assert _transport(client).closed.is_set()


async def test_failed_close_after_a_cancelled_reconnect_does_not_fail_the_last_request(
    server: I232Server, caplog: pytest.LogCaptureFixture
) -> None:
    """The last request closes the retired client; a close error is logged, the request still succeeds."""
    server.close_error = RuntimeError("Event loop is closed")
    old = await Manager.get_client()
    server.close_error = None
    in_flight = asyncio.create_task(old.query(HOLD))
    await asyncio.wait_for(_transport(old).held.wait(), 1)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(Manager.reconnect(), 0.1)

    _transport(old).release.set()
    with caplog.at_level(logging.WARNING):
        response = await asyncio.wait_for(in_flight, 1)

    assert response.is_ok
    assert not old.is_connected
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.exc_info is not None]
    assert warnings and isinstance(warnings[0].exc_info[1], RuntimeError)  # type: ignore[index]
