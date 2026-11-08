"""Unit tests for #619 (v2): a ``changes()`` resume whose ``get_client()`` fails after ``stop()``.

After a poll fails because ``reconnect()`` or ``close_connection()`` closed
the stream's client, ``ChangeModelStream._replacement_feed()`` resolves the
client again through ``get_client()``. #569 made a stream stopped during that
call end once the call returned; #617 (#610) did the same for the first client
build when the call raised. On the resume path, a call that raised still sent
its error out of a stream the caller had already stopped. A stopped stream now
ends there too, with no new poll and no new feed; a stream that was not
stopped still gets the error, and ``CancelledError`` or any other
``BaseException`` is never swallowed.

No server is involved: the change-feed half reuses the in-memory feed of the
#569 tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from src.surreal_orm import SurrealDBConnectionManager as Manager
from src.surreal_orm.connection_manager import SurrealDbConnectionError
from src.surreal_orm.live import ChangeModelStream
from tests.test_issue_569_unit import I569Item, I569Server, _next, isolated_registry, server

__all__ = ["isolated_registry", "server"]


class I619Abort(BaseException):
    """A ``BaseException`` that is not an ``Exception``, raised by the held resume."""


async def _stream_on_a_closed_client(server: I569Server) -> tuple[ChangeModelStream[I569Item], Any]:
    """A stream that read one event, then lost its client to ``close_connection()``."""
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    server.feed.write("i569_items", {"id": "i569_items:a"})
    assert (await _next(stream)).record_id == "i569_items:a"
    old = await Manager.get_client()
    await Manager.close_connection()
    assert not old.is_connected
    return stream, old


def _hold_then_raise(
    monkeypatch: pytest.MonkeyPatch, make_error: Callable[[], BaseException]
) -> tuple[asyncio.Event, asyncio.Event]:
    """Make the resume's ``get_client()`` wait for a gate, then raise ``make_error()``."""
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def held_get_client(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await gate.wait()
        raise make_error()

    monkeypatch.setattr(Manager, "get_client", held_get_client)
    return entered, gate


async def _start_resume(
    server: I569Server,
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[], BaseException],
) -> tuple[ChangeModelStream[I569Item], Any, asyncio.Future[Any], asyncio.Event, int]:
    """Drive the stream into a resume held in ``get_client()``; return what the checks need."""
    stream, old = await _stream_on_a_closed_client(server)
    built = len(server.built)
    entered, gate = _hold_then_raise(monkeypatch, make_error)
    server.feed.write("i569_items", {"id": "i569_items:b"})
    pending = asyncio.ensure_future(_next(stream))
    await asyncio.wait_for(entered.wait(), 1)
    assert len(server.built) == built
    return stream, old, pending, gate, len(server.feed.queries)


async def test_changes_stopped_during_a_resume_that_raises_end_quietly(
    server: I569Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop() while the resume's get_client() is pending, which then raises: the iteration ends."""
    stream, old, pending, gate, polls = await _start_resume(
        server, monkeypatch, lambda: SurrealDbConnectionError("Can't connect to the database.")
    )
    feed = stream._feed
    built = len(server.built)
    stream.stop()
    gate.set()

    with pytest.raises(StopAsyncIteration):
        await pending
    with pytest.raises(StopAsyncIteration):
        await _next(stream)

    assert len(server.feed.queries) == polls, "a stream stopped during its resume polled the change feed"
    assert len(server.built) == built
    assert stream._feed is feed, "a stopped stream built a new change feed"
    assert stream._connection is old, "a stopped stream moved to a new client"


async def test_changes_resume_error_still_reaches_a_running_stream(server: I569Server, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume's get_client() that raises on a stream nobody stopped raises from __anext__(), as before."""
    stream, old, pending, gate, polls = await _start_resume(
        server, monkeypatch, lambda: SurrealDbConnectionError("Can't connect to the database.")
    )
    gate.set()

    with pytest.raises(SurrealDbConnectionError, match="Can't connect"):
        await pending
    assert len(server.feed.queries) == polls
    assert stream._connection is old


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "running"])
async def test_changes_resume_still_propagates_cancellation(
    server: I569Server, monkeypatch: pytest.MonkeyPatch, stopped: bool
) -> None:
    """A CancelledError out of the held resume is not swallowed, stopped or not."""
    stream, _old, pending, gate, polls = await _start_resume(server, monkeypatch, asyncio.CancelledError)
    if stopped:
        stream.stop()
    gate.set()

    await asyncio.wait({pending}, timeout=2)
    assert pending.cancelled(), f"the cancellation was swallowed: {pending!r}"
    assert len(server.feed.queries) == polls


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped", "running"])
async def test_changes_resume_still_propagates_base_exceptions(
    server: I569Server, monkeypatch: pytest.MonkeyPatch, stopped: bool
) -> None:
    """A BaseException that is not an Exception still leaves the stream, stopped or not."""
    stream, _old, pending, gate, polls = await _start_resume(server, monkeypatch, I619Abort)
    if stopped:
        stream.stop()
    gate.set()

    await asyncio.wait({pending}, timeout=2)
    assert pending.done() and not pending.cancelled()
    assert isinstance(pending.exception(), I619Abort)
    assert len(server.feed.queries) == polls
