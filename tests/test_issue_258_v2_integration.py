"""Integration tests for #258 (v2), against the pinned SurrealDB 2.x server.

* ``live(diff=True)`` used to send ``LIVE SELECT * FROM t WHERE ... DIFF``,
  a parse error: no DIFF subscription could start.
* An ``int`` record id at the edges of the signed 64-bit range renders a
  literal that designates the bound record.
* A naive ``datetime`` bound as a parameter is read as UTC.
* Over WebSocket, an out-of-range ``Decimal`` raises ``ValueError`` and the
  connection keeps working.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk import SurrealDB
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.streaming.live_select import LiveSelectStream
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_258_v2"
WS_URL = SURREALDB_URL.replace("http://", "ws://").replace("https://", "wss://")


class I258Item(BaseSurrealModel):
    """A record a DIFF live query watches."""

    model_config = SurrealConfigDict(table_name="i258_items")

    id: str | None = None
    tag: str = Field(default="")
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
    await client.query("REMOVE TABLE IF EXISTS i258_items; DEFINE TABLE i258_items SCHEMALESS;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i258_items;")


async def _write(sql: str, variables: dict[str, Any] | None = None) -> None:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(sql, variables or {})
    assert response.is_ok, response.raw


@pytest.mark.integration
async def test_live_diff_with_where_receives_the_update() -> None:
    """``filter(...).live(diff=True)`` starts, and the matching update arrives as a patch."""
    await _write("CREATE i258_items:decoy SET tag = 'other', score = 0; CREATE i258_items:match SET tag = 'hot', score = 0;")
    async with I258Item.objects().filter(tag="hot").live(diff=True) as stream:
        await _write("UPDATE i258_items:decoy SET score = 1;")
        await _write("UPDATE i258_items:match SET score = 2;")
        event = await asyncio.wait_for(anext(stream), timeout=5.0)
    assert event.changed_fields == ["score"], event.raw


@pytest.mark.integration
async def test_sdk_live_select_diff_with_where_starts() -> None:
    """The SDK stream sends ``LIVE SELECT DIFF FROM ... WHERE ...``, which the server accepts."""
    async with SurrealDB.ws(WS_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE) as ws:
        await ws.signin(SURREALDB_USER, SURREALDB_PASS)
        async with LiveSelectStream(ws, "i258_items", where="tag = $t", params={"t": "hot"}, diff=True) as stream:
            assert stream.is_active
            await _write("CREATE i258_items:decoy SET tag = 'other', score = 0;")
            await _write("CREATE i258_items:match SET tag = 'hot', score = 0;")
            await _write("UPDATE i258_items:decoy SET score = 1;")
            await _write("UPDATE i258_items:match SET score = 2;")
            changes = [await asyncio.wait_for(anext(stream), timeout=5.0) for _ in range(2)]
        assert [c.action.value for c in changes] == ["CREATE", "UPDATE"], changes
        assert changes[1].changed_fields == ["score"], changes[1]


@pytest.mark.integration
@pytest.mark.parametrize("record_id", [2**63 - 1, -(2**63)], ids=["i64-max", "i64-min"])
async def test_i64_edge_record_literal_is_the_bound_record(record_id: int) -> None:
    """The literal at each edge is the numeric record the bound ``RecordId`` names."""
    literal = LiveSelectStream._format_value(RecordId("t", record_id))
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(
        f"RETURN [{literal} = $r, type::is::number(record::id({literal}))]", {"r": RecordId("t", record_id)}
    )
    assert response.is_ok, response.raw
    assert response.results[0].result == [True, True], literal


@pytest.mark.integration
async def test_naive_datetime_binds_as_utc() -> None:
    """A naive ``datetime`` reaches the server as the same wall time in UTC."""
    client = await SurrealDBConnectionManager.get_client()
    with pytest.warns(RuntimeWarning, match="naive datetime"):
        response = await client.query(
            "RETURN [$naive = $aware, $naive]",
            {"naive": datetime(2026, 2, 1, 12, 30), "aware": datetime(2026, 2, 1, 12, 30, tzinfo=UTC)},
        )
    assert response.is_ok, response.raw
    equal, stored = response.results[0].result
    assert equal is True, stored
    assert stored == datetime(2026, 2, 1, 12, 30, tzinfo=UTC)


@pytest.mark.integration
async def test_websocket_out_of_range_decimal_raises_value_error() -> None:
    """The caller gets the encoder's ``ValueError``, and the connection still works."""
    async with SurrealDB.ws(WS_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE) as ws:
        await ws.signin(SURREALDB_USER, SURREALDB_PASS)
        with pytest.raises(ValueError, match="out of range"):
            await ws.query("RETURN $p", {"p": Decimal("1E+29")})
        assert ws._pending == {}
        response = await ws.query("RETURN $p", {"p": Decimal("1.5")})
        assert response.is_ok, response.raw
        assert response.results[0].result == Decimal("1.5")
