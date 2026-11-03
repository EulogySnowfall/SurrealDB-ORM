"""Integration tests for #466 (v2), against the pinned SurrealDB 2.x server.

In DIFF mode a live notification carries JSON patches instead of the record.
``live(diff=True)`` on a model with a required field used to end the stream
with a ``ValidationError`` at the first notification, and the event named no
record (``record_id == ""``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, LiveAction, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk import SurrealDB
from surreal_sdk.streaming.live_select import LiveSelectStream
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_466_v2"
WS_URL = SURREALDB_URL.replace("http://", "ws://").replace("https://", "wss://")


class I466Player(BaseSurrealModel):
    """A watched record whose model has a required field."""

    model_config = SurrealConfigDict(table_name="i466_players")

    id: str | None = None
    name: str
    team: str
    score: int = 0


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, Any]:
    """Point the ORM at a database of its own for this module."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def clean_table() -> AsyncGenerator[None, Any]:
    """A fresh table: a LIVE SELECT needs it to exist."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i466_players; DEFINE TABLE i466_players SCHEMALESS;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i466_players;")


async def _write(sql: str) -> None:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    assert response.is_ok, response.raw


@pytest.mark.integration
async def test_orm_live_diff_event_names_the_record() -> None:
    """The DIFF event names the updated record and lists the patched field."""
    await _write(
        "CREATE i466_players:decoy SET name = 'Bob', team = 'blue', score = 0;"
        "CREATE i466_players:alice SET name = 'Alice', team = 'red', score = 0;"
    )
    async with I466Player.objects().filter(team="red").live(diff=True) as stream:
        await _write("UPDATE i466_players:decoy SET score = 1;")
        await _write("UPDATE i466_players:alice SET score = 7;")
        event = await asyncio.wait_for(anext(stream), timeout=5.0)
    assert event.action == LiveAction.UPDATE
    assert event.record_id == "i466_players:alice", event.raw
    assert event.changed_fields == ["score"], event.raw
    assert event.instance.id == "alice"


@pytest.mark.integration
async def test_orm_live_without_diff_still_yields_the_full_record() -> None:
    """``diff=False`` keeps validating the full record into the instance."""
    await _write("CREATE i466_players:alice SET name = 'Alice', team = 'red', score = 0;")
    async with I466Player.objects().filter(team="red").live() as stream:
        await _write("UPDATE i466_players:alice SET score = 7;")
        event = await asyncio.wait_for(anext(stream), timeout=5.0)
    assert event.record_id == "i466_players:alice"
    assert event.instance.name == "Alice"
    assert event.instance.score == 7
    assert event.changed_fields == []


@pytest.mark.integration
async def test_server_diff_notification_carries_the_record() -> None:
    """The pinned 2.x server names the record in a DIFF notification; the SDK reads it."""
    await _write("CREATE i466_players:alice SET name = 'Alice', team = 'red', score = 0;")
    async with SurrealDB.ws(WS_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE) as ws:
        await ws.signin(SURREALDB_USER, SURREALDB_PASS)
        async with LiveSelectStream(ws, "i466_players", diff=True) as stream:
            await _write("UPDATE i466_players:alice SET score = 7;")
            change = await asyncio.wait_for(anext(stream), timeout=5.0)
    assert change.record_id == "i466_players:alice"
    assert change.changed_fields == ["score"]
