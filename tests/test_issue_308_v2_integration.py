"""Integration tests for #308: change feeds deliver changes on the v2 pin (SurrealDB 2.x).

``ChangeFeedStream`` sent ``SINCE '<text>'``, a parse error on SurrealDB 2.7.0,
and its polling loops swallowed the error: ``QuerySet.changes()`` never
delivered anything and never raised. Each test defines fresh ``CHANGEFEED``
tables, writes after the stream has started where it matters, and waits for
the change with a bounded timeout rather than a sleep.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
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

SURREALDB_DATABASE = "test_issue_308_v2"
TABLES = ("i308_items", "i308_other", "i308_orig")

#: Bounded waits; a poll interval of 50 ms delivers well within them.
START_TIMEOUT = 10.0
EVENT_TIMEOUT = 5.0


class I308Item(BaseSurrealModel):
    """A record on a plain ``CHANGEFEED`` table."""

    model_config = SurrealConfigDict(table_name="i308_items")

    id: str | None = None
    name: str = Field(default="")


class I308Orig(BaseSurrealModel):
    """A record on a ``CHANGEFEED ... INCLUDE ORIGINAL`` table."""

    model_config = SurrealConfigDict(table_name="i308_orig")

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


#: Retryable conflicts in a row before a write gives up and fails the test.
_MAX_WRITE_CONFLICTS = 50


def _is_retryable_conflict(raw: Any) -> bool:
    """Whether a rejected write carries the server's retryable-conflict sentence."""
    return "can be retried" in str(raw).lower()


def _applied_any(raw: Any) -> bool:
    """Whether a statement of the response succeeded, so re-sending the query would run it twice."""
    items = raw if isinstance(raw, list) else [raw]
    return any(isinstance(item, dict) and item.get("status") == "OK" for item in items)


async def _query_retrying(
    client: Any,
    sql: str,
    *,
    max_conflicts: int = _MAX_WRITE_CONFLICTS,
    backoff: float = 0.01,
) -> None:
    """Run ``sql`` and fail with the raw response unless it succeeds.

    On 2.7.0, concurrent writes to different ``CHANGEFEED`` tables of one
    database now and then end in a conflict the server reports as retryable
    ("This transaction can be retried"). Such a reply is re-sent after
    ``backoff`` seconds, as a client should, unless a statement of the query
    was applied (re-sending would run it twice). The ``max_conflicts``-th
    conflict in a row fails, as does any other error.
    """
    conflicts = 0
    while True:
        response = await client.query(sql)
        if not response.is_ok and _is_retryable_conflict(response.raw) and not _applied_any(response.raw):
            conflicts += 1
            assert conflicts < max_conflicts, response.raw
            await asyncio.sleep(backoff)
            continue
        assert response.is_ok, response.raw
        return


async def _write(
    sql: str,
    *,
    client: Any = None,
    max_conflicts: int = _MAX_WRITE_CONFLICTS,
    backoff: float = 0.01,
) -> None:
    """Run a write of the test, retrying a retryable conflict (see ``_query_retrying()``)."""
    await _query_retrying(client if client is not None else await _client(), sql, max_conflicts=max_conflicts, backoff=backoff)


@pytest.fixture(autouse=True)
async def fresh_tables() -> AsyncGenerator[None, Any]:
    """New change-feed tables per test."""
    removes = " ".join(f"REMOVE TABLE IF EXISTS {table};" for table in TABLES)
    await _write(
        removes + " DEFINE TABLE i308_items CHANGEFEED 1h; DEFINE TABLE i308_other CHANGEFEED 1h;"
        " DEFINE TABLE i308_orig CHANGEFEED 1h INCLUDE ORIGINAL;"
    )
    yield
    await _write(removes)


async def _now() -> int:
    """The versionstamp after the last entry already in the feed.

    The feed outlives ``REMOVE TABLE``, so ``since=0`` also reads what earlier
    tests wrote to a table of the same name.
    """
    return await ChangeFeedStream(await _client(), "i308_items")._resolve_now()


def _updated_ids(entries: list[dict[str, Any]]) -> list[str]:
    return [str(change["update"]["id"]) for entry in entries for change in entry["changes"] if "update" in change]


async def _wait_started(cursor: Callable[[], Any]) -> None:
    """Wait until a stream has resolved its start (its cursor is set)."""

    async def poll() -> None:
        while cursor() is None:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), START_TIMEOUT)


async def _next(iterator: AsyncIterator[Any]) -> Any:
    return await asyncio.wait_for(anext(iterator), EVENT_TIMEOUT)


class TestGetChanges:
    async def test_since_zero_returns_the_entries(self) -> None:
        await _write("CREATE i308_items:a SET name = 'a'; CREATE i308_items:b SET name = 'b';")
        entries = await ChangeFeedStream(await _client(), "i308_items").get_changes(since=0)
        assert _updated_ids(entries)[-2:] == ["i308_items:a", "i308_items:b"]

    async def test_since_an_entry_versionstamp_includes_it_and_one_past_it_does_not(self) -> None:
        start = await _now()
        await _write("CREATE i308_items:a SET name = 'a';")
        await _write("CREATE i308_items:b SET name = 'b';")
        feed = ChangeFeedStream(await _client(), "i308_items")
        entries = await feed.get_changes(since=start)
        assert _updated_ids(entries) == ["i308_items:a", "i308_items:b"]
        last = entries[-1]["versionstamp"]
        assert _updated_ids(await feed.get_changes(since=last)) == ["i308_items:b"]
        assert await feed.get_changes(since=last + 1) == []

    @pytest.mark.parametrize(
        "since",
        [
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 1),
            "2026-01-01T00:00:00Z",
            "2026-01-01",
        ],
        ids=["dt", "naive-dt", "iso", "date"],
    )
    async def test_datetime_starts_are_accepted_by_the_server(self, since: Any) -> None:
        """The statement parses (no ``ChangeFeedError``); what it selects depends on the storage engine."""
        await _write("CREATE i308_items:a SET name = 'a';")
        entries = await ChangeFeedStream(await _client(), "i308_items").get_changes(since=since)
        assert isinstance(entries, list)


class TestQuerySetChanges:
    async def test_default_start_delivers_a_later_save_as_update(self) -> None:
        await _write("CREATE i308_items:old SET name = 'old';")
        stream = I308Item.objects().changes(poll_interval=0.05)
        first = asyncio.ensure_future(_next(stream))
        try:
            await _wait_started(lambda: stream.cursor)
            await I308Item(id="new", name="new").save()
            event = await first
        finally:
            first.cancel()
            stream.stop()
        assert event.action == LiveAction.UPDATE
        assert event.record_id == "i308_items:new"
        assert event.instance.id == "new"
        assert event.instance.name == "new"

    async def test_since_cursor_resumes_after_the_last_delivered_change(self) -> None:
        stream = I308Item.objects().changes(since=await _now(), poll_interval=0.05)
        await I308Item(id="one", name="one").save()
        assert (await _next(stream)).record_id == "i308_items:one"
        cursor = stream.cursor
        stream.stop()
        assert cursor is not None

        await I308Item(id="two", name="two").save()
        resumed = I308Item.objects().changes(since=cursor, poll_interval=0.05)
        try:
            assert (await _next(resumed)).record_id == "i308_items:two"
        finally:
            resumed.stop()

    async def test_cursor_mid_transaction_resumes_the_rest_of_it(self) -> None:
        """One transaction is one entry; a checkpoint after its first event must not skip the others."""
        stream = I308Item.objects().changes(since=await _now(), poll_interval=0.05)
        await _write("BEGIN; CREATE i308_items:ta; CREATE i308_items:tb; CREATE i308_items:tc; COMMIT;")
        try:
            first = await _next(stream)
            cursor = stream.cursor
        finally:
            stream.stop()
        assert first.record_id == "i308_items:ta"
        assert cursor is not None

        resumed = I308Item.objects().changes(since=cursor, poll_interval=0.05)
        try:
            ids = [(await _next(resumed)).record_id for _ in range(3)]
            after_entry = resumed.cursor
        finally:
            resumed.stop()
        assert ids == ["i308_items:ta", "i308_items:tb", "i308_items:tc"]
        assert after_entry is not None and int(after_entry) == int(cursor) + 1

    async def test_a_change_behind_a_page_of_other_tables_is_delivered(self) -> None:
        """``LIMIT`` counts the entries of every table; ``batch_size`` of them used to stall the stream."""
        stream = I308Item.objects().changes(poll_interval=0.05, batch_size=5)
        first = asyncio.ensure_future(_next(stream))
        try:
            await _wait_started(lambda: stream.cursor)
            await _write(" ".join(f"CREATE i308_other:o{i} SET i = {i};" for i in range(25)))
            await I308Item(id="late", name="late").save()
            event = await first
        finally:
            first.cancel()
            stream.stop()
        assert event.record_id == "i308_items:late"

    async def test_include_original_update_is_delivered(self) -> None:
        """There an update arrives as ``{current, update: [patch]}``."""
        await I308Orig(id="x", name="before").save()
        stream = I308Orig.objects().changes(poll_interval=0.05)
        first = asyncio.ensure_future(_next(stream))
        try:
            await _wait_started(lambda: stream.cursor)
            await _write("UPDATE i308_orig:x SET name = 'after';")
            event = await first
        finally:
            first.cancel()
            stream.stop()
        assert event.action == LiveAction.UPDATE
        assert event.record_id == "i308_orig:x"
        assert event.instance.name == "after"


class TestSdkStreams:
    async def test_each_change_is_delivered_once(self) -> None:
        feed = ChangeFeedStream(await _client(), "i308_items", poll_interval=0.05)
        stream = feed.stream(since=await _now())
        await _write("CREATE i308_items:a SET name = 'a';")
        await _write("CREATE i308_items:b SET name = 'b';")
        seen: list[str] = []
        try:
            while len(seen) < 2:
                seen.extend(_updated_ids([await _next(stream)]))
            await _write("CREATE i308_items:c SET name = 'c';")
            while len(seen) < 3:
                seen.extend(_updated_ids([await _next(stream)]))
        finally:
            await stream.aclose()  # type: ignore[attr-defined]
        assert seen == ["i308_items:a", "i308_items:b", "i308_items:c"]

    async def test_multi_table_feed_advances_each_table(self) -> None:
        feed = MultiTableChangeFeed(await _client(), ["i308_items", "i308_other"], poll_interval=0.05)
        stream = feed.stream(since=str(await _now()))
        await _write("CREATE i308_items:a; CREATE i308_other:a;")
        got: list[tuple[str, str]] = []
        try:
            while len(got) < 2:
                table, entry = await _next(stream)
                got.extend((table, record) for record in _updated_ids([entry]))
            await _write("CREATE i308_other:b;")
            while len(got) < 3:
                table, entry = await _next(stream)
                got.extend((table, record) for record in _updated_ids([entry]))
        finally:
            await stream.aclose()  # type: ignore[attr-defined]
        assert sorted(got[:2]) == [("i308_items", "i308_items:a"), ("i308_other", "i308_other:a")]
        assert got[2] == ("i308_other", "i308_other:b")


async def _write_other_table(
    client: Any,
    written: list[int],
    *,
    max_conflicts: int = _MAX_WRITE_CONFLICTS,
    backoff: float = 0.01,
) -> None:
    """Create ``i308_other:w<n>`` records one after another until cancelled.

    ``written[0]`` counts the successful writes only. A write rejected with the
    server's retryable-conflict sentence is retried with the same id (see
    ``_query_retrying()``); a persistent conflict or any other error fails with
    the raw response.
    """
    while True:
        await _query_retrying(
            client, f"CREATE i308_other:w{written[0]} SET i = {written[0]};", max_conflicts=max_conflicts, backoff=backoff
        )
        written[0] += 1


#: Writes the background writer makes before the block starts.
_WARM_UP_WRITES = 5


@asynccontextmanager
async def _busy_other_table(*, client: Any = None, progress_timeout: float = EVENT_TIMEOUT) -> AsyncIterator[list[int]]:
    """Keep writing to ``i308_other`` in the background while the block runs.

    Yields a one-item list holding the number of successful writes so far. The
    writer is running (``_WARM_UP_WRITES`` writes in) when the block starts.
    After the block, at least one more write must land within
    ``progress_timeout`` seconds: the feed kept moving while the block read it,
    however long the read took. The writer is then cancelled and awaited; a
    writer that died fails the test with its own error.
    """
    written = [0]
    writer_client = client if client is not None else await _client()
    task = asyncio.create_task(_write_other_table(writer_client, written))

    async def reached(target: int) -> None:
        while written[0] < target:
            if task.done():
                # Raise the writer's own error rather than a bare "stopped".
                task.result()
            assert not task.done(), task
            await asyncio.sleep(0.01)

    try:
        await asyncio.wait_for(reached(_WARM_UP_WRITES), START_TIMEOUT)
        start = written[0]
        yield written
        try:
            await asyncio.wait_for(reached(start + 1), progress_timeout)
        except TimeoutError:
            raise AssertionError(
                f"no write landed on i308_other within {progress_timeout}s of the block (stuck at {written[0]})"
            ) from None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TestBusyFeed:
    """Reads end while another table of the database keeps being written to.

    A read stops on the first window the server did not cut with its ``LIMIT``.
    Only an empty window stopping it would never end here: the writer adds an
    entry to the database feed between any two polls.
    """

    async def test_get_changes_since_zero_returns_while_another_table_is_written(self) -> None:
        await _write("CREATE i308_items:one SET name = 'one';")
        async with _busy_other_table():
            feed = ChangeFeedStream(await _client(), "i308_items")
            # A limit no read reaches, so only the end of the feed stops it.
            entries = await asyncio.wait_for(feed.get_changes(since=0, limit=1_000_000), START_TIMEOUT)
        assert _updated_ids(entries)[-1] == "i308_items:one"
        assert all(not record.startswith("i308_other:") for record in _updated_ids(entries))

    async def test_default_start_stream_delivers_its_first_change_while_another_table_is_written(self) -> None:
        await _write("CREATE i308_items:old SET name = 'old';")
        async with _busy_other_table():
            feed = ChangeFeedStream(await _client(), "i308_items", poll_interval=0.05)
            stream = feed.stream()
            first = asyncio.ensure_future(asyncio.wait_for(anext(stream), START_TIMEOUT))
            try:
                await _wait_started(lambda: feed.cursor)
                await _write("CREATE i308_items:new SET name = 'new';")
                entry = await first
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
                await stream.aclose()  # type: ignore[attr-defined]
        assert _updated_ids([entry]) == ["i308_items:new"]


class _FakeResponse:
    """The two attributes of a ``QueryResponse`` the writer reads."""

    def __init__(self, raw: Any, *, ok: bool) -> None:
        self.raw = raw
        self.is_ok = ok


class _FakeClient:
    """Replays canned responses and records every query it receives."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.queries: list[str] = []

    async def query(self, sql: str) -> _FakeResponse:
        self.queries.append(sql)
        return self.responses.pop(0)


_OK = _FakeResponse([{"status": "OK", "result": []}], ok=True)
_CONFLICT = _FakeResponse(
    [{"status": "ERR", "result": "Transaction conflict: Resource busy. This transaction can be retried"}], ok=False
)
_OTHER_ERROR = _FakeResponse([{"status": "ERR", "result": "Database record `i308_other:w1` already exists"}], ok=False)


class TestBusyFeedWriter:
    """The background writer of ``_busy_other_table()``, against a fake client."""

    async def test_retries_a_retryable_conflict_with_the_same_id_and_fails_on_another_error(self) -> None:
        client = _FakeClient([_CONFLICT, _CONFLICT, _OK, _OTHER_ERROR])
        written = [0]
        with pytest.raises(AssertionError, match="already exists"):
            await _write_other_table(client, written, backoff=0)
        assert written == [1]
        assert client.queries == [
            "CREATE i308_other:w0 SET i = 0;",
            "CREATE i308_other:w0 SET i = 0;",
            "CREATE i308_other:w0 SET i = 0;",
            "CREATE i308_other:w1 SET i = 1;",
        ]

    async def test_fails_when_conflicts_in_a_row_reach_the_cap(self) -> None:
        client = _FakeClient([_CONFLICT, _OK, _CONFLICT, _CONFLICT, _CONFLICT])
        written = [0]
        with pytest.raises(AssertionError, match="can be retried"):
            await _write_other_table(client, written, max_conflicts=3, backoff=0)
        assert written == [1]
        # The success resets the count: the cap is three conflicts in a row.
        assert len(client.queries) == 5
        assert client.responses == []


_PARTIAL_CONFLICT = _FakeResponse(
    [
        {"status": "OK", "result": [{"id": "i308_items:a"}]},
        {"status": "ERR", "result": "Transaction conflict: Resource busy. This transaction can be retried"},
    ],
    ok=False,
)


class TestWriteRetry:
    """``_write()``, the tests' own writes, against a fake client."""

    async def test_retries_a_retryable_conflict_then_succeeds(self) -> None:
        client = _FakeClient([_CONFLICT, _CONFLICT, _OK])
        await _write("CREATE i308_items:new SET name = 'new';", client=client, backoff=0)
        assert client.queries == ["CREATE i308_items:new SET name = 'new';"] * 3
        assert client.responses == []

    async def test_fails_on_another_error_without_retrying(self) -> None:
        client = _FakeClient([_OTHER_ERROR, _OK])
        with pytest.raises(AssertionError, match="already exists"):
            await _write("CREATE i308_items:new;", client=client, backoff=0)
        assert len(client.queries) == 1

    async def test_fails_when_conflicts_in_a_row_reach_the_cap(self) -> None:
        client = _FakeClient([_CONFLICT, _CONFLICT, _CONFLICT, _OK])
        with pytest.raises(AssertionError, match="can be retried"):
            await _write("CREATE i308_items:new;", client=client, max_conflicts=3, backoff=0)
        assert len(client.queries) == 3

    async def test_does_not_replay_a_query_whose_earlier_statement_was_applied(self) -> None:
        """Re-sending would run the applied ``CREATE`` again, so the conflict fails at once."""
        client = _FakeClient([_PARTIAL_CONFLICT, _OK])
        with pytest.raises(AssertionError, match="can be retried"):
            await _write("CREATE i308_items:a; CREATE i308_items:b;", client=client, backoff=0)
        assert len(client.queries) == 1


class _AlwaysOkClient:
    """Answers every query with success after ``delay`` seconds; hangs for good once ``stall_after`` is reached."""

    def __init__(self, *, delay: float = 0.001, stall_after: int | None = None) -> None:
        self.delay = delay
        self.stall_after = stall_after
        self.count = 0

    async def query(self, sql: str) -> _FakeResponse:
        if self.stall_after is not None and self.count >= self.stall_after:
            await asyncio.Event().wait()
        self.count += 1
        await asyncio.sleep(self.delay)
        return _OK


class TestBusyOtherTableProgress:
    """The progress check of ``_busy_other_table()`` does not depend on how long the block takes."""

    async def test_a_block_shorter_than_one_write_still_passes(self) -> None:
        async with _busy_other_table(client=_AlwaysOkClient(delay=0.05)) as written:
            start = written[0]
        assert written[0] > start

    async def test_no_write_after_the_yield_fails_within_the_bound(self) -> None:
        with pytest.raises(AssertionError, match="no write landed"):
            async with _busy_other_table(client=_AlwaysOkClient(stall_after=5), progress_timeout=0.2):
                pass

    async def test_a_writer_that_died_fails_with_its_own_error(self) -> None:
        client = _FakeClient([_OK] * 5 + [_OTHER_ERROR])
        with pytest.raises(AssertionError, match="already exists"):
            async with _busy_other_table(client=client, progress_timeout=1.0):
                await asyncio.sleep(0.05)
