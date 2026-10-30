"""Integration test for #266 (v2 backport of #232 / #260): ``reconnect()`` does not break requests already running.

``reconnect()`` used to close the shared HTTP client before building the new
one, so a request still waiting for its response failed with
``ConnectionError: Request failed``. Here several tasks keep queries and saves
running while ``reconnect()`` is called repeatedly; every one of them must
succeed. Each query sleeps on the server so that some are always in flight when
the client is swapped.

An HTTP transaction sends its statements only at commit, so it used the old
client without any request in flight: one open across the swap failed at commit
with ``Not connected`` and none of its writes reached the database.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_266"

WORKERS = 4
RECONNECTS = 8


class I266Note(BaseSurrealModel):
    """One record per completed worker iteration."""

    model_config = SurrealConfigDict(table_name="i266_notes")

    id: str | None = None
    worker: int = 0
    n: int = 0


class I266TxItem(BaseSurrealModel):
    """Records written inside transactions that span a reconnect()."""

    model_config = SurrealConfigDict(table_name="i266_tx_items")

    id: str | None = None
    v: int = 0


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
    client = await SurrealDBConnectionManager.get_client()
    # Defined up front: concurrent first writes that each create the table
    # implicitly conflict on the server ("Transaction conflict: Write conflict").
    await client.query(
        "REMOVE TABLE IF EXISTS i266_notes; DEFINE TABLE i266_notes SCHEMALESS; "
        "REMOVE TABLE IF EXISTS i266_tx_items; DEFINE TABLE i266_tx_items SCHEMALESS;"
    )
    yield
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i266_notes; REMOVE TABLE IF EXISTS i266_tx_items;")
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.mark.integration
async def test_concurrent_requests_survive_repeated_reconnect() -> None:
    """Queries and saves running while reconnect() swaps the client all succeed."""
    stop = asyncio.Event()
    completed = 0

    async def worker(w: int) -> None:
        nonlocal completed
        n = 0
        while not stop.is_set():
            client = await SurrealDBConnectionManager.get_client()
            response = await client.query("SLEEP 30ms; RETURN $n;", {"n": n})
            assert response.is_ok, response.raw
            assert response.results[-1].result == n
            await I266Note(id=f"w{w}_{n}", worker=w, n=n).save()
            n += 1
            completed += 1

    tasks = [asyncio.create_task(worker(w)) for w in range(WORKERS)]
    installed: list[Any] = []
    try:
        for _ in range(RECONNECTS):
            await asyncio.sleep(0.05)
            installed.append(await SurrealDBConnectionManager.reconnect())
    finally:
        stop.set()
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert failures == [], f"requests failed across reconnect(): {failures!r}"
    assert len({id(c) for c in installed}) == RECONNECTS
    assert all(c is not None for c in installed)
    assert installed[-1].is_connected
    assert all(not c.is_connected for c in installed[:-1])
    assert completed > 0
    assert await I266Note.objects().count() == completed


async def _tx_item_ids() -> set[str]:
    return {str(item.id) for item in await I266TxItem.objects().all()}


@pytest.mark.integration
async def test_transaction_spanning_a_concurrent_reconnect_commits() -> None:
    """A transaction open while another task calls reconnect() commits all its writes."""
    previous = await SurrealDBConnectionManager.get_client()

    async def body() -> None:
        async with await SurrealDBConnectionManager.transaction() as tx:
            await I266TxItem(id="span_a", v=1).save(tx=tx)
            await asyncio.sleep(0.3)  # application work while the client is swapped
            await I266TxItem(id="span_b", v=2).save(tx=tx)

    async def refresher() -> Any:
        await asyncio.sleep(0.05)
        return await SurrealDBConnectionManager.reconnect()

    _, new = await asyncio.wait_for(asyncio.gather(body(), refresher()), 10)

    assert new is not previous and new.is_connected
    assert not previous.is_connected
    assert {"span_a", "span_b"} <= await _tx_item_ids()


@pytest.mark.integration
async def test_reconnect_inside_a_transaction_commits() -> None:
    """reconnect() called from a transaction body returns at once, and the transaction still commits."""
    previous = await SurrealDBConnectionManager.get_client()

    async with await SurrealDBConnectionManager.transaction() as tx:
        await I266TxItem(id="inside_a", v=1).save(tx=tx)
        new = await asyncio.wait_for(SurrealDBConnectionManager.reconnect(), 5)
        await I266TxItem(id="inside_b", v=2).save(tx=tx)

    assert new is not previous and new is not None and new.is_connected
    assert not previous.is_connected
    assert {"inside_a", "inside_b"} <= await _tx_item_ids()
