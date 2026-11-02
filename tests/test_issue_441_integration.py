"""Integration tests for #441: DIFF live queries start, and receive patches.

Every builder used to send ``LIVE SELECT * FROM t [WHERE ...] DIFF``, a parse
error on SurrealDB 3.x, so no ``diff=True`` subscription could start. The
DIFF form is ``LIVE SELECT DIFF FROM t [WHERE ...]``. A DIFF notification
carries JSON patches as its result and names the record in its ``record``
field.

Each test opens the stream with a filter, updates a record that must *not*
match, then one that must, and checks that the first notification is the
matching record's patch. Notifications on one connection arrive in order.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.streaming.live_select import LiveAction, LiveChange
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER, SURREALDB_WS_URL

SURREALDB_DATABASE = "test_issue_441"


class I441Item(BaseSurrealModel):
    """A record with a required filter field and a field the tests change.

    ``tag`` is required on purpose: a DIFF event must not validate the
    (empty) record against the model.
    """

    model_config = SurrealConfigDict(table_name="i441_items")

    id: str | None = None
    tag: str
    n: int | None = None


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
    """A fresh table holding one matching record (``a``) and one decoy (``b``)."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i441_items; DEFINE TABLE i441_items SCHEMALESS;")
    await client.query("CREATE i441_items:a SET tag = 'x', n = 1; CREATE i441_items:b SET tag = 'y', n = 1;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i441_items;")


@pytest.fixture
async def ws() -> AsyncGenerator[WebSocketConnection, Any]:
    """A signed-in WebSocket connection on the module's database."""
    conn = WebSocketConnection(SURREALDB_WS_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    try:
        yield conn
    finally:
        await conn.close()


async def _update_decoy_then_match() -> None:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query("UPDATE i441_items:b SET n = 2; UPDATE i441_items:a SET n = 2;")
    assert response.is_ok, response.raw


@pytest.mark.integration
async def test_queryset_live_diff_with_filter_receives_update() -> None:
    """``filter(...).live(diff=True)`` starts and delivers the matching record's patch."""
    async with I441Item.objects().filter(tag="x").live(diff=True) as stream:
        await _update_decoy_then_match()
        event = await asyncio.wait_for(anext(stream), timeout=5.0)

    assert event.action == LiveAction.UPDATE
    assert event.record_id == "i441_items:a"
    assert event.changed_fields == ["n"]
    # A DIFF notification carries patches, not the record: the instance has the id only.
    assert isinstance(event.instance, I441Item)
    assert event.instance.id == "a"


@pytest.mark.integration
async def test_sdk_live_select_diff_with_filter_receives_update(ws: WebSocketConnection) -> None:
    """``live_select(..., diff=True)`` with a bound filter starts and delivers the patch."""
    async with ws.live_select("i441_items", where="tag = $t", params={"t": "x"}, diff=True) as stream:
        await _update_decoy_then_match()
        change = await asyncio.wait_for(anext(stream), timeout=5.0)

    assert change.action == LiveAction.UPDATE
    assert change.record_id == "i441_items:a"
    assert change.changed_fields == ["n"]


@pytest.mark.integration
async def test_websocket_live_diff_receives_patch(ws: WebSocketConnection) -> None:
    """The callback API ``WebSocketConnection.live(diff=True)`` starts too."""
    received: asyncio.Queue[Any] = asyncio.Queue()

    async def callback(data: Any) -> None:
        await received.put(data)

    live_id = await ws.live("i441_items", callback, diff=True)
    try:
        client = await SurrealDBConnectionManager.get_client()
        await client.query("UPDATE i441_items:a SET n = 3;")
        notification = await asyncio.wait_for(received.get(), timeout=5.0)
    finally:
        await ws.kill(live_id)

    change = LiveChange.from_dict(notification)
    assert change.record_id == "i441_items:a"
    assert change.changed_fields == ["n"]


@pytest.mark.integration
async def test_websocket_encoding_error_is_the_callers_error(ws: WebSocketConnection) -> None:
    """A value the encoder refuses raises as is; the connection keeps working."""
    with pytest.raises(TypeError):
        await ws.query("RETURN $x", {"x": object()})
    assert ws._pending == {}

    response = await ws.query("RETURN 1")
    assert response.is_ok
    assert response.results[0].result == 1
