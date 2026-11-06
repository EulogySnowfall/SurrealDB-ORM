"""Integration test for #270: ``QuerySet.changes()`` keeps delivering across ``reconnect()``.

A ``changes()`` stream on the default connection resolved its HTTP client
once. ``reconnect()`` closes that client, so the next poll failed with
``Not connected`` and the stream ended with ``ChangeFeedError``. It now
resolves the client again and resumes from its cursor: every change is
delivered once, in order, on both sides of the swap.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_sdk.streaming.change_feed import ChangeFeedStream
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_270"


class I270Event(BaseSurrealModel):
    """Records written on both sides of a reconnect()."""

    model_config = SurrealConfigDict(table_name="i270_events")

    id: str | None = None
    n: int = 0


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, Any]:
    """Point the ORM at a database of its own, with a change feed on the table."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i270_events; DEFINE TABLE i270_events SCHEMALESS CHANGEFEED 1h;")
    yield
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i270_events;")
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


async def _now() -> int:
    """The versionstamp after the last entry already in the feed: a start before the first write."""
    return await ChangeFeedStream(await SurrealDBConnectionManager.get_client(), "i270_events")._resolve_now()


async def _next(stream: Any) -> Any:
    return await asyncio.wait_for(anext(stream), 10)


@pytest.mark.integration
async def test_changes_stream_survives_reconnect() -> None:
    """Changes written before, around and after reconnect() each arrive once, in order."""
    stream = I270Event.objects().changes(since=await _now(), poll_interval=0.05)
    await I270Event(id="before", n=1).save()
    received = [await _next(stream)]
    previous = await SurrealDBConnectionManager.get_client()

    # Written before the swap but read only after it: the cursor carries it over.
    await I270Event(id="around", n=2).save()
    new = await asyncio.wait_for(SurrealDBConnectionManager.reconnect(), 10)
    assert new is not previous and not previous.is_connected

    await I270Event(id="after1", n=3).save()
    await I270Event(id="after2", n=4).save()
    received += [await _next(stream) for _ in range(3)]
    stream.stop()

    assert [event.instance.n for event in received] == [1, 2, 3, 4]
    assert [event.instance.id for event in received] == ["before", "around", "after1", "after2"]
