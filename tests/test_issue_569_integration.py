"""Integration test for #569 (v2 backport of #567): ``changes()`` keeps delivering across ``reconnect()``.

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

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, _raise_on_statement_error
from surreal_sdk.streaming.change_feed import ChangeFeedStream
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

_DATABASE = "test_issue_569"


class I569Event(BaseSurrealModel):
    """Records written on both sides of a reconnect()."""

    model_config = SurrealConfigDict(table_name="i569_events")

    id: str | None = None
    n: int = 0


@pytest.fixture(scope="module", autouse=True)
async def _connection() -> AsyncGenerator[None, None]:
    """Point the ORM at a database of its own, with a change feed on the table."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        _DATABASE,
    )
    client = await SurrealDBConnectionManager.get_client()
    query = "REMOVE TABLE IF EXISTS i569_events; DEFINE TABLE i569_events SCHEMALESS CHANGEFEED 1h;"
    _raise_on_statement_error(await client.query(query), query)
    yield
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i569_events;")
    await SurrealDBConnectionManager.unset_connection()


async def _now() -> int:
    """The versionstamp after the last entry already in the feed: a start before the first write."""
    return await ChangeFeedStream(await SurrealDBConnectionManager.get_client(), "i569_events")._resolve_now()


async def _next(stream: Any) -> Any:
    return await asyncio.wait_for(anext(stream), 10)


async def test_changes_stream_survives_reconnect() -> None:
    """Changes written before, around and after reconnect() each arrive once, in order."""
    stream = I569Event.objects().changes(since=await _now(), poll_interval=0.05)
    try:
        await I569Event(id="before", n=1).save()
        received = [await _next(stream)]
        previous = await SurrealDBConnectionManager.get_client()

        # Written before the swap but read only after it: the cursor carries it over.
        await I569Event(id="around", n=2).save()
        new = await asyncio.wait_for(SurrealDBConnectionManager.reconnect(), 10)
        assert new is not previous and not previous.is_connected

        await I569Event(id="after1", n=3).save()
        await I569Event(id="after2", n=4).save()
        received += [await _next(stream) for _ in range(3)]
    finally:
        stream.stop()

    assert [event.instance.n for event in received] == [1, 2, 3, 4]
    assert [event.instance.id for event in received] == ["before", "around", "after1", "after2"]
