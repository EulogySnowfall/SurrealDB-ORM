"""Integration test for #232: ``reconnect()`` does not break requests already running.

``reconnect()`` used to close the shared HTTP client before building the new
one, so a request still waiting for its response failed with
``ConnectionError: Request failed``. Here several tasks keep queries and saves
running while ``reconnect()`` is called repeatedly; every one of them must
succeed. Each query sleeps on the server so that some are always in flight when
the client is swapped.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_232"

WORKERS = 4
RECONNECTS = 8


class I232Note(BaseSurrealModel):
    """One record per completed worker iteration."""

    model_config = SurrealConfigDict(table_name="i232_notes")

    id: str | None = None
    worker: int = 0
    n: int = 0


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
    await client.query("REMOVE TABLE IF EXISTS i232_notes; DEFINE TABLE i232_notes SCHEMALESS;")
    yield
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i232_notes;")
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
            await I232Note(id=f"w{w}_{n}", worker=w, n=n).save()
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
    assert await I232Note.objects().count() == completed
