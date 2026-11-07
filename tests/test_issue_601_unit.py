"""Unit tests for #601 (v2): ``stop()`` during the first client build of ``changes()``.

``ChangeModelStream._stream_events()`` read ``_stopped`` before
``_ensure_feed()`` (#577) but not after it. A ``stop()`` that arrived while the
first ``__anext__()`` was waiting in ``get_client()`` found no feed to stop,
so ``_ensure_feed()`` then built one and ``feed.stream()`` sent a poll before
the stream noticed. The stream now ends once the client is resolved, without
polling.

No server is involved: the change-feed half reuses the in-memory feed of the
#569 tests.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.surreal_orm import SurrealDBConnectionManager as Manager
from tests.test_issue_569_unit import I569Item, I569Server, _next, isolated_registry, server

__all__ = ["isolated_registry", "server"]


async def test_changes_stopped_during_the_first_client_build_end_without_polling(
    server: I569Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stop() while the first get_client() is pending ends the iteration: no change-feed query is sent."""
    server.feed.write("i569_items", {"id": "i569_items:a"})
    real_get_client = Manager.get_client
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def held_get_client(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await gate.wait()
        return await real_get_client(*args, **kwargs)

    monkeypatch.setattr(Manager, "get_client", held_get_client)
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
    assert stream.cursor is None
