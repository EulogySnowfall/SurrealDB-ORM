"""Integration tests for #346: change-feed reads end while other tables are written.

``_resolve_now()`` and ``get_changes()`` stopped only on an empty read of the
database feed, and re-read the last versionstamp of every window, so one write
anywhere between two reads kept them going: under a steady writer on another
table, the default start never resolved and ``get_changes()`` never returned.

On SurrealDB 3.3 a window with fewer entries than ``LIMIT`` can still end in the
middle of a multi-table versionstamp, so the tests also commit transactions that
write the watched table and another one, with a small scan limit that makes such
cuts likely, and check that every watched record is delivered exactly once.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.streaming import change_feed as change_feed_module
from surreal_sdk.streaming.change_feed import ChangeFeedStream
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = "test_issue_346"
WATCHED = "i346_watched"
OTHER = "i346_other"
SIDE = "i346_side"
TABLES = (WATCHED, OTHER, SIDE)

#: What "a few seconds" means here; before the fix these calls never returned.
BOUND = 15.0
WRITERS = 3


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


async def _client() -> HTTPConnection:
    client = await SurrealDBConnectionManager.get_client()
    assert isinstance(client, HTTPConnection)
    return client


async def _write(sql: str) -> None:
    response = await (await _client()).query(sql)
    assert response.is_ok, response.raw


@pytest.fixture(autouse=True)
async def fresh_tables() -> AsyncGenerator[None, Any]:
    removes = " ".join(f"REMOVE TABLE IF EXISTS {table};" for table in TABLES)
    await _write(removes + "".join(f" DEFINE TABLE {table} CHANGEFEED 1h;" for table in TABLES))
    yield
    await _write(removes)


@pytest.fixture
def small_scan_limit(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A scan limit of a few units, so windows often end inside a versionstamp."""
    monkeypatch.setattr(change_feed_module, "_SCAN_LIMIT", 7)
    yield


@asynccontextmanager
async def _busy_writers() -> AsyncIterator[list[int]]:
    """Writers creating records on another table until the block exits; yields their counts."""
    stop = asyncio.Event()
    counts = [0] * WRITERS

    async def writer(n: int) -> None:
        client = await _client()
        while not stop.is_set():
            await client.query(f"CREATE {OTHER} SET n = {n};")
            counts[n] += 1
            await asyncio.sleep(0.002)

    tasks = [asyncio.create_task(writer(n)) for n in range(WRITERS)]
    try:
        # Let the load build up before the reads start.
        while sum(counts) < 2 * WRITERS:
            await asyncio.sleep(0.01)
        yield counts
    finally:
        stop.set()
        await asyncio.gather(*tasks)


async def _write_watched(run: str, count: int) -> list[str]:
    """Write *count* watched records, each in a transaction that also writes two other tables."""
    ids = []
    for i in range(count):
        record = f"{WATCHED}:{run}_{i}"
        await _write(f"BEGIN; CREATE {OTHER} SET k = {i}; CREATE {record}; CREATE {SIDE} SET k = {i}; COMMIT;")
        ids.append(record)
    return ids


def _watched_ids(entries: list[dict[str, Any]], run: str) -> list[str]:
    return [
        str(change["update"]["id"])
        for entry in entries
        for change in entry["changes"]
        if isinstance(change.get("update"), dict) and str(change["update"].get("id", "")).startswith(f"{WATCHED}:{run}_")
    ]


class TestBusyWriter:
    async def test_get_changes_since_zero_returns_each_entry_once(self, small_scan_limit: None) -> None:
        run = "r" + uuid.uuid4().hex[:8]
        async with _busy_writers() as counts:
            written = await _write_watched(run, 12)
            feed = ChangeFeedStream(await _client(), WATCHED)
            entries = await asyncio.wait_for(feed.get_changes(since=0, limit=10_000), BOUND)
            writes_during_read = sum(counts)
        got = _watched_ids(entries, run)
        assert Counter(got) == Counter(written), got
        assert got == written
        assert writes_during_read > 0

    async def test_default_start_resolves_and_streams_each_later_entry_once(self, small_scan_limit: None) -> None:
        run = "r" + uuid.uuid4().hex[:8]
        await _write_watched(run + "old", 3)
        async with _busy_writers():
            now = await asyncio.wait_for(ChangeFeedStream(await _client(), WATCHED)._resolve_now(), BOUND)
            assert now > 0

            feed = ChangeFeedStream(await _client(), WATCHED, poll_interval=0.02)
            stream = feed.stream()
            try:
                first = asyncio.ensure_future(anext(stream))

                async def started() -> None:
                    while feed.cursor is None:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(started(), BOUND)
                written = await _write_watched(run, 6)
                entries = [await asyncio.wait_for(first, BOUND)]
                while len(_watched_ids(entries, run)) < len(written):
                    entries.append(await asyncio.wait_for(anext(stream), BOUND))
            finally:
                await stream.aclose()
        assert _watched_ids(entries, run) == written
        assert _watched_ids(entries, run + "old") == []

    async def test_resolve_now_returns_with_the_full_scan_limit(self) -> None:
        """The default scan limit, as shipped."""
        async with _busy_writers():
            now = await asyncio.wait_for(ChangeFeedStream(await _client(), WATCHED)._resolve_now(), BOUND)
        assert now > 0
