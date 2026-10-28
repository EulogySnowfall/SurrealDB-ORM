"""Integration tests for #281: change feeds deliver changes on the pinned server.

``ChangeFeedStream`` sent ``SINCE '<text>'``, a parse error on every server
line, and its polling loops swallowed the error: ``QuerySet.changes()`` never
delivered anything and never raised. Each test defines a fresh ``CHANGEFEED``
table, writes after the stream has started where it matters, and waits for the
change with a bounded timeout rather than a sleep.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.streaming.change_feed import ChangeFeedStream, MultiTableChangeFeed
from surreal_sdk.streaming.live_query import LiveAction
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = "test_issue_281"
TABLES = ("i281_items", "i281_other", "i281_orig")

#: Bounded waits; a poll interval of 50 ms delivers well within them.
START_TIMEOUT = 10.0
EVENT_TIMEOUT = 5.0


class I281Item(BaseSurrealModel):
    """A record on a plain ``CHANGEFEED`` table."""

    model_config = SurrealConfigDict(table_name="i281_items")

    id: str | None = None
    name: str = Field(default="")


class I281Orig(BaseSurrealModel):
    """A record on a ``CHANGEFEED … INCLUDE ORIGINAL`` table."""

    model_config = SurrealConfigDict(table_name="i281_orig")

    id: str | None = None
    name: str = Field(default="")


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
    """New change-feed tables per test (``DEFINE TABLE`` fails on an existing one)."""
    removes = " ".join(f"REMOVE TABLE IF EXISTS {table};" for table in TABLES)
    await _write(
        removes + " DEFINE TABLE i281_items CHANGEFEED 1h; DEFINE TABLE i281_other CHANGEFEED 1h;"
        " DEFINE TABLE i281_orig CHANGEFEED 1h INCLUDE ORIGINAL;"
    )
    yield
    await _write(removes)


async def _now() -> int:
    """The versionstamp after the last entry already in the feed.

    The feed outlives ``REMOVE TABLE``, so ``since=0`` also reads what earlier
    tests wrote to a table of the same name.
    """
    return await ChangeFeedStream(await _client(), "i281_items")._resolve_now()


def _updated_ids(entries: list[dict[str, Any]]) -> list[str]:
    return [str(change["update"]["id"]) for entry in entries for change in entry["changes"] if "update" in change]


async def _wait_started(cursor: Any) -> None:
    """Wait until a stream has resolved its start (its cursor is set)."""

    async def poll() -> None:
        while cursor() is None:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), START_TIMEOUT)


async def _next(iterator: AsyncIterator[Any]) -> Any:
    return await asyncio.wait_for(anext(iterator), EVENT_TIMEOUT)


class TestGetChanges:
    async def test_since_zero_returns_the_entries(self) -> None:
        await _write("CREATE i281_items:a SET name = 'a'; CREATE i281_items:b SET name = 'b';")
        entries = await ChangeFeedStream(await _client(), "i281_items").get_changes(since=0)
        assert _updated_ids(entries)[-2:] == ["i281_items:a", "i281_items:b"]

    @pytest.mark.parametrize(
        "since", [datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01T00:00:00Z", "2026-01-01"], ids=["dt", "iso", "date"]
    )
    async def test_datetime_starts_are_accepted_by_the_server(self, since: Any) -> None:
        """The statement parses (no ``ChangeFeedError``); what it selects depends on the storage engine."""
        await _write("CREATE i281_items:a SET name = 'a';")
        entries = await ChangeFeedStream(await _client(), "i281_items").get_changes(since=since)
        assert isinstance(entries, list)


class TestQuerySetChanges:
    async def test_default_start_delivers_a_later_save_as_update(self) -> None:
        await _write("CREATE i281_items:old SET name = 'old';")
        stream = I281Item.objects().changes(poll_interval=0.05)
        first = asyncio.ensure_future(_next(stream))
        try:
            await _wait_started(lambda: stream.cursor)
            await I281Item(id="new", name="new").save()
            event = await first
        finally:
            first.cancel()
            stream.stop()
        assert event.action == LiveAction.UPDATE
        assert event.record_id == "i281_items:new"
        assert event.instance.id == "new"
        assert event.instance.name == "new"

    async def test_since_cursor_resumes_after_the_last_delivered_change(self) -> None:
        stream = I281Item.objects().changes(since=await _now(), poll_interval=0.05)
        await I281Item(id="one", name="one").save()
        assert (await _next(stream)).record_id == "i281_items:one"
        cursor = stream.cursor
        stream.stop()
        assert cursor is not None

        await I281Item(id="two", name="two").save()
        resumed = I281Item.objects().changes(since=cursor, poll_interval=0.05)
        try:
            assert (await _next(resumed)).record_id == "i281_items:two"
        finally:
            resumed.stop()

    async def test_cursor_mid_transaction_resumes_the_rest_of_it(self) -> None:
        """One transaction is one entry; a checkpoint after its first event must not skip the others."""
        stream = I281Item.objects().changes(since=await _now(), poll_interval=0.05)
        await _write("BEGIN; CREATE i281_items:ta; CREATE i281_items:tb; CREATE i281_items:tc; COMMIT;")
        try:
            first = await _next(stream)
            cursor = stream.cursor
        finally:
            stream.stop()
        assert first.record_id == "i281_items:ta"
        assert cursor is not None

        resumed = I281Item.objects().changes(since=cursor, poll_interval=0.05)
        try:
            ids = [(await _next(resumed)).record_id for _ in range(3)]
            after_entry = resumed.cursor
        finally:
            resumed.stop()
        assert ids == ["i281_items:ta", "i281_items:tb", "i281_items:tc"]
        assert after_entry is not None and int(after_entry) == int(cursor) + 1

    async def test_a_change_behind_a_page_of_other_tables_is_delivered(self) -> None:
        """``LIMIT`` counts the entries of every table; ``batch_size`` of them used to stall the stream."""
        stream = I281Item.objects().changes(poll_interval=0.05, batch_size=5)
        first = asyncio.ensure_future(_next(stream))
        try:
            await _wait_started(lambda: stream.cursor)
            await _write(" ".join(f"CREATE i281_other:o{i} SET i = {i};" for i in range(25)))
            await I281Item(id="late", name="late").save()
            event = await first
        finally:
            first.cancel()
            stream.stop()
        assert event.record_id == "i281_items:late"

    async def test_include_original_update_is_delivered(self) -> None:
        """There an update arrives as ``{current, update: [patch]}``."""
        await I281Orig(id="x", name="before").save()
        stream = I281Orig.objects().changes(poll_interval=0.05)
        first = asyncio.ensure_future(_next(stream))
        try:
            await _wait_started(lambda: stream.cursor)
            await _write("UPDATE i281_orig:x SET name = 'after';")
            event = await first
        finally:
            first.cancel()
            stream.stop()
        assert event.action == LiveAction.UPDATE
        assert event.record_id == "i281_orig:x"
        assert event.instance.name == "after"


class TestSdkStreams:
    async def test_each_change_is_delivered_once(self) -> None:
        feed = ChangeFeedStream(await _client(), "i281_items", poll_interval=0.05)
        stream = feed.stream(since=await _now())
        await _write("CREATE i281_items:a SET name = 'a';")
        await _write("CREATE i281_items:b SET name = 'b';")
        seen: list[str] = []
        try:
            while len(seen) < 2:
                seen.extend(_updated_ids([await _next(stream)]))
            await _write("CREATE i281_items:c SET name = 'c';")
            while len(seen) < 3:
                seen.extend(_updated_ids([await _next(stream)]))
        finally:
            await stream.aclose()
        assert seen == ["i281_items:a", "i281_items:b", "i281_items:c"]

    async def test_multi_table_feed_advances_each_table(self) -> None:
        feed = MultiTableChangeFeed(await _client(), ["i281_items", "i281_other"], poll_interval=0.05)
        stream = feed.stream(since=str(await _now()))
        await _write("CREATE i281_items:a; CREATE i281_other:a;")
        got: list[tuple[str, str]] = []
        try:
            while len(got) < 2:
                table, entry = await _next(stream)
                got.extend((table, record) for record in _updated_ids([entry]))
            await _write("CREATE i281_other:b;")
            while len(got) < 3:
                table, entry = await _next(stream)
                got.extend((table, record) for record in _updated_ids([entry]))
        finally:
            await stream.aclose()
        assert sorted(got[:2]) == [("i281_items", "i281_items:a"), ("i281_other", "i281_other:a")]
        assert got[2] == ("i281_other", "i281_other:b")
