"""Unit tests for #610 (v2): a first client build of ``changes()`` that fails after ``stop()``.

Since #601, ``ChangeModelStream._stream_events()`` reads ``_stopped`` after
the first ``_ensure_feed()``, so a ``stop()`` that arrives while that build is
waiting in ``get_client()`` ends the stream once the build returns. When the
build raised instead (the server cannot be reached, say), the exception still
left a stream the caller had already stopped. A stopped stream now ends there
too, whether the build returns or raises an ``Exception``; a stream that was
not stopped still gets the exception, and ``CancelledError`` or any other
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
from tests.test_issue_569_unit import I569Item, I569Server, _next, isolated_registry, server

__all__ = ["isolated_registry", "server"]


class I610Abort(BaseException):
    """A ``BaseException`` that is not an ``Exception``, raised by the held build."""


def _hold_then_raise(
    monkeypatch: pytest.MonkeyPatch, make_error: Callable[[], BaseException]
) -> tuple[asyncio.Event, asyncio.Event]:
    """Make ``get_client()`` wait for a gate, then raise ``make_error()``."""
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def held_get_client(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await gate.wait()
        raise make_error()

    monkeypatch.setattr(Manager, "get_client", held_get_client)
    return entered, gate


async def test_changes_stopped_during_a_first_build_that_raises_end_quietly(
    server: I569Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop() while the first get_client() is pending, which then raises: the iteration ends, no poll."""
    server.feed.write("i569_items", {"id": "i569_items:a"})
    entered, gate = _hold_then_raise(monkeypatch, lambda: SurrealDbConnectionError("Can't connect to the database."))
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    pending = asyncio.ensure_future(_next(stream))
    await asyncio.wait_for(entered.wait(), 1)
    stream.stop()
    gate.set()

    with pytest.raises(StopAsyncIteration):
        await pending
    with pytest.raises(StopAsyncIteration):
        await _next(stream)

    assert server.feed.queries == [], "a stream stopped during its first client build polled the change feed"
    assert server.built == []
    assert stream.cursor is None


async def test_changes_first_build_error_still_reaches_a_running_stream(
    server: I569Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A get_client() that raises on a stream nobody stopped raises from __anext__(), as before."""

    async def failing_get_client(*args: Any, **kwargs: Any) -> Any:
        raise SurrealDbConnectionError("Can't connect to the database.")

    monkeypatch.setattr(Manager, "get_client", failing_get_client)
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)

    with pytest.raises(SurrealDbConnectionError, match="Can't connect"):
        await _next(stream)
    assert server.feed.queries == []


async def test_changes_first_build_error_held_until_after_a_late_start_reaches_the_caller(
    server: I569Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held build that raises on a stream still running when it fails raises from __anext__()."""
    entered, gate = _hold_then_raise(monkeypatch, lambda: SurrealDbConnectionError("Can't connect to the database."))
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    pending = asyncio.ensure_future(_next(stream))
    await asyncio.wait_for(entered.wait(), 1)
    gate.set()

    with pytest.raises(SurrealDbConnectionError, match="Can't connect"):
        await pending
    assert server.feed.queries == []


async def test_changes_stopped_during_the_first_build_still_propagate_cancellation(
    server: I569Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CancelledError out of the held build is not swallowed, even on a stopped stream."""
    entered, gate = _hold_then_raise(monkeypatch, asyncio.CancelledError)
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    pending = asyncio.ensure_future(_next(stream))
    await asyncio.wait_for(entered.wait(), 1)
    stream.stop()
    gate.set()

    await asyncio.wait({pending}, timeout=2)
    assert pending.cancelled(), f"the cancellation was swallowed: {pending!r}"
    assert server.feed.queries == []


async def test_changes_stopped_during_the_first_build_still_propagate_base_exceptions(
    server: I569Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BaseException that is not an Exception still leaves a stopped stream."""
    entered, gate = _hold_then_raise(monkeypatch, I610Abort)
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    pending = asyncio.ensure_future(_next(stream))
    await asyncio.wait_for(entered.wait(), 1)
    stream.stop()
    gate.set()

    await asyncio.wait({pending}, timeout=2)
    assert pending.done() and not pending.cancelled()
    assert isinstance(pending.exception(), I610Abort)
    assert server.feed.queries == []
