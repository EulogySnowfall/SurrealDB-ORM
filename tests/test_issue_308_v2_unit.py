"""Unit tests for #308: change feed ``SINCE`` rendering and once-only delivery on v2.

``ChangeFeedStream`` sent ``SHOW CHANGES ... SINCE '<text>'``, which SurrealDB
2.7.0 rejects at parse time, and its polling loops swallowed the error, so
``QuerySet.changes()`` never delivered a change and never raised.

The fake server below models ``SHOW CHANGES`` as measured on SurrealDB 2.7.0
(in-memory storage):

* an entry's ``versionstamp`` is the commit version shifted left by 16 bits
  (``65536``, ``131072``, ...), while ``SINCE <n>`` takes the commit version
  itself: ``SINCE 2`` starts at the entry ``131072``, ``SINCE 131072`` selects
  nothing;
* ``LIMIT`` counts the entries of every table before ``FOR TABLE`` keeps the
  ones of the table (100 without a ``LIMIT``, never more than 1000);
* a ``d'...'`` start is resolved through the server's coarse timestamp map; on
  a fresh in-memory server it selects the whole feed;
* a quoted string is a parse error.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.surreal_orm.live import ChangeModelStream
from src.surreal_orm.model_base import BaseSurrealModel
from src.surreal_sdk.exceptions import ChangeFeedError, QueryError
from src.surreal_sdk.protocol.cbor import RecordId
from src.surreal_sdk.streaming import change_feed as change_feed_module
from src.surreal_sdk.streaming.change_feed import ChangeFeedStream, MultiTableChangeFeed, _render_since
from src.surreal_sdk.streaming.live_select import LiveAction
from src.surreal_sdk.types import QueryResponse

#: Distance between two consecutive commit versions in an entry's versionstamp.
STEP = 1 << 16


class I308Player(BaseSurrealModel):
    id: str | None = None
    name: str = ""
    age: int = 0


# ==================== SINCE rendering ====================


class TestRenderSince:
    """``SINCE`` takes a datetime literal or an unquoted commit version, never a quoted string."""

    def test_aware_datetime_becomes_utc_datetime_literal(self) -> None:
        since = datetime(2026, 10, 27, 14, 30, 5, 123456, tzinfo=timezone(timedelta(hours=2)))
        assert _render_since(since) == "d'2026-10-27T12:30:05.123456Z'"

    def test_utc_datetime_has_a_single_zone_designator(self) -> None:
        """``isoformat() + "Z"`` used to give ``...+00:00Z``."""
        assert _render_since(datetime(2026, 10, 27, 12, tzinfo=UTC)) == "d'2026-10-27T12:00:00.000000Z'"

    def test_naive_datetime_is_read_as_utc(self) -> None:
        assert _render_since(datetime(2026, 10, 27, 12, 0, 0)) == "d'2026-10-27T12:00:00.000000Z'"

    def test_iso_string_with_zone_designator(self) -> None:
        assert _render_since("2026-10-27T12:00:00Z") == "d'2026-10-27T12:00:00.000000Z'"

    def test_iso_string_with_offset_is_converted_to_utc(self) -> None:
        assert _render_since("2026-10-27T12:00:00-04:00") == "d'2026-10-27T16:00:00.000000Z'"

    def test_iso_string_without_offset_is_read_as_utc(self) -> None:
        assert _render_since("2026-10-27T12:00:00") == "d'2026-10-27T12:00:00.000000Z'"

    def test_date_only_string_is_midnight_utc(self) -> None:
        """The example the ``QuerySet.changes()`` docstring used to show."""
        assert _render_since("2026-01-01") == "d'2026-01-01T00:00:00.000000Z'"

    def test_zero_reads_the_whole_feed(self) -> None:
        assert _render_since(0) == "0"
        assert _render_since("0") == "0"

    def test_an_entry_versionstamp_becomes_its_commit_version(self) -> None:
        """``SINCE`` includes the entry whose versionstamp is given."""
        assert _render_since(STEP) == "1"
        assert _render_since(5 * STEP) == "5"
        assert _render_since(str(5 * STEP)) == "5"

    def test_one_past_an_entry_is_the_next_commit_version(self) -> None:
        """A cursor is the last versionstamp plus one: the next entry, never the last again."""
        assert _render_since(5 * STEP + 1) == "6"
        assert _render_since(str(5 * STEP + 1)) == "6"

    def test_largest_versionstamp_is_accepted(self) -> None:
        assert _render_since(2**64 - 1) == str(2**48)

    @pytest.mark.parametrize(
        "since",
        ["", "   ", "yesterday", "-1", "1.5", "0x10", "2026-13-01", "0' OR true", "1; REMOVE TABLE orders"],
    )
    def test_invalid_string_raises_value_error(self, since: str) -> None:
        with pytest.raises(ValueError):
            _render_since(since)

    @pytest.mark.parametrize("since", [-1, 2**64])
    def test_out_of_range_versionstamp_raises_value_error(self, since: int) -> None:
        with pytest.raises(ValueError):
            _render_since(since)

    @pytest.mark.parametrize("since", [True, 1.5, b"0", [0]])
    def test_unsupported_type_raises_type_error(self, since: Any) -> None:
        with pytest.raises(TypeError):
            _render_since(since)


# ==================== A model of the 2.7.0 server ====================


_SHOW_RE = re.compile(r"SHOW CHANGES FOR (?:TABLE (\w+)|DATABASE) SINCE (\S+)(?: LIMIT (\d+))?")


class FakeChangeFeedServer:
    """In-memory ``SHOW CHANGES`` with the semantics measured on SurrealDB 2.7.0."""

    def __init__(self) -> None:
        #: ``(commit version, table, changes)``, ordered.
        self.keys: list[tuple[int, str, list[dict[str, Any]]]] = []
        self.queries: list[str] = []
        self.next_version = 1
        self.fail_with: Exception | None = None
        self.statement_error: str | None = None
        #: A stream that polls without making progress fails here instead of hanging the test.
        self.max_queries = 500
        #: Entries written to ``busy`` before each query: concurrent writers that never stop.
        self.writes_per_query = 0

    def write(self, table: str, *records: dict[str, Any], version: int | None = None) -> int:
        """Record one transaction on *table* and return the entry's versionstamp."""
        if version is None:
            version = self.next_version
            self.next_version += 1
        self.keys.append((version, table, [{"update": record} for record in records] or [{"update": {}}]))
        self.keys.sort(key=lambda key: (key[0], key[1]))
        return version * STEP

    def show(self, table: str | None, since: str, limit: int | None) -> list[dict[str, Any]]:
        if since.startswith("'"):
            raise QueryError("Parse error: Unexpected token `a strand`, expected a version stamp or a date-time")
        start = 0 if since.startswith("d'") else int(since)
        scanned = [key for key in self.keys if key[0] >= start][: min(limit or 100, 1000)]
        # One entry per (commit version, table), as 2.7.0 reports them: LIMIT counts entries.
        return [
            {"versionstamp": version * STEP, "changes": list(changes)}
            for version, tb, changes in scanned
            if table is None or tb == table
        ]

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        self.queries.append(sql)
        if len(self.queries) > self.max_queries:
            raise AssertionError("too many SHOW CHANGES queries: the stream is spinning")
        if self.fail_with is not None:
            raise self.fail_with
        for _ in range(self.writes_per_query):
            self.write("busy", {"id": f"busy:{self.next_version}"})
        results: list[dict[str, Any]] = []
        for statement in (part.strip() for part in sql.split(";")):
            if not statement:
                continue
            if statement in ("BEGIN", "COMMIT"):
                results.append({"status": "OK", "result": None})
                continue
            match = _SHOW_RE.fullmatch(statement)
            if match is None:
                raise QueryError(f"Parse error in {statement!r}")
            if self.statement_error is not None:
                results.append({"status": "ERR", "result": self.statement_error})
                continue
            table, since, limit = match.groups()
            results.append({"status": "OK", "result": self.show(table, since, int(limit) if limit else None)})
        return QueryResponse.from_rpc_result(results)


def _feed(server: FakeChangeFeedServer, table: str = "orders", **kwargs: Any) -> ChangeFeedStream:
    return ChangeFeedStream(server, table, **kwargs)  # type: ignore[arg-type]


async def _take(iterator: AsyncIterator[Any], count: int, timeout: float = 2.0) -> list[Any]:
    """The next *count* items, failing instead of hanging."""
    items: list[Any] = []

    async def collect() -> None:
        while len(items) < count:
            items.append(await anext(iterator))

    await asyncio.wait_for(collect(), timeout)
    return items


def _ids(entries: list[dict[str, Any]]) -> list[Any]:
    return [change["update"].get("id") for entry in entries for change in entry["changes"]]


async def _wait_cursor(feed: ChangeFeedStream) -> None:
    for _ in range(200):
        if feed.cursor is not None:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the stream never resolved its start")


class TestChangeFeedQueries:
    """The statements sent, the cursor and paging past other tables' entries."""

    async def test_no_statement_quotes_since(self) -> None:
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:1"})
        await _feed(server).get_changes(since="2026-01-01")
        await _feed(server).get_changes(since=0)
        assert server.queries
        assert all("SINCE '" not in query for query in server.queries)

    async def test_invalid_since_sends_no_query(self) -> None:
        server = FakeChangeFeedServer()
        feed = _feed(server)
        with pytest.raises(ValueError):
            await feed.get_changes(since="yesterday")
        with pytest.raises(ValueError):
            await anext(feed.stream(since="yesterday"))
        with pytest.raises(ValueError):
            await anext(feed.stream_batch(since="not a date"))
        with pytest.raises(ValueError):
            await anext(MultiTableChangeFeed(server, ["orders"]).stream(since="soon"))  # type: ignore[arg-type]
        assert server.queries == []

    async def test_get_changes_since_zero_returns_the_table_entries(self) -> None:
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:1"})
        server.write("other", {"id": "other:1"})
        server.write("orders", {"id": "orders:2"})
        entries = await _feed(server).get_changes(since=0)
        assert _ids(entries) == ["orders:1", "orders:2"]

    async def test_get_changes_since_an_entry_includes_it(self) -> None:
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:1"})
        second = server.write("orders", {"id": "orders:2"})
        assert _ids(await _feed(server).get_changes(since=second)) == ["orders:2"]
        assert _ids(await _feed(server).get_changes(since=second + 1)) == []

    async def test_get_changes_without_since_and_cursor_returns_nothing(self) -> None:
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:1"})
        assert await _feed(server).get_changes() == []
        assert server.queries == []

    async def test_get_changes_pages_past_other_tables(self) -> None:
        """``LIMIT`` counts every table's entries, so one page can hold none of ours."""
        server = FakeChangeFeedServer()
        for i in range(1250):
            server.write("other", {"id": f"other:{i}"})
        server.write("orders", {"id": "orders:1"})
        entries = await _feed(server, batch_size=10).get_changes(since=0)
        assert _ids(entries) == ["orders:1"]

    async def test_get_changes_returns_while_other_tables_keep_writing(self) -> None:
        """A window that was not cut is the end: it used to read until an empty window."""
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:1"})
        server.writes_per_query = 3
        entries = await asyncio.wait_for(_feed(server).get_changes(since=0), 2.0)
        assert _ids(entries) == ["orders:1"]
        assert len(server.queries) == 1

    async def test_get_changes_respects_limit(self) -> None:
        server = FakeChangeFeedServer()
        for i in range(5):
            server.write("orders", {"id": f"orders:{i}"})
        entries = await _feed(server).get_changes(since=0, limit=2)
        assert _ids(entries) == ["orders:0", "orders:1"]

    async def test_get_changes_raises_on_a_statement_error(self) -> None:
        """It used to return ``[]`` for a non-OK statement result."""
        server = FakeChangeFeedServer()
        server.statement_error = "IAM error: Not enough permissions"
        with pytest.raises(ChangeFeedError, match="Not enough permissions"):
            await _feed(server).get_changes(since=0)

    async def test_get_changes_wraps_a_transport_error(self) -> None:
        server = FakeChangeFeedServer()
        server.fail_with = ConnectionError("connection reset")
        with pytest.raises(ChangeFeedError, match="connection reset"):
            await _feed(server).get_changes(since=0)

    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ChangeFeedStream(MagicMock(), "orders", batch_size=0)


class TestChangeFeedStreams:
    async def test_default_start_skips_existing_entries(self) -> None:
        """A ``d'<now>'`` start replays the whole feed on 2.7.0; the default must not."""
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:old"})
        feed = _feed(server, poll_interval=0.01)
        stream = feed.stream()
        first = asyncio.ensure_future(anext(stream))
        await _wait_cursor(feed)
        server.write("orders", {"id": "orders:new"})
        entry = await asyncio.wait_for(first, 2.0)
        assert _ids([entry]) == ["orders:new"]
        await stream.aclose()

    async def test_default_start_resolves_while_the_feed_keeps_growing(self) -> None:
        """Under steady writes the feed is never empty; "now" used to wait for an empty window."""
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:old"})
        server.writes_per_query = 3
        feed = _feed(server, poll_interval=0.01)
        stream = feed.stream()
        first = asyncio.ensure_future(anext(stream))
        await _wait_cursor(feed)
        # "now" took a single read of the feed; the polls that follow are the stream's.
        assert [q for q in server.queries if not q.startswith("BEGIN")] == [server.queries[0]]
        server.write("orders", {"id": "orders:new"})
        entry = await asyncio.wait_for(first, 2.0)
        assert _ids([entry]) == ["orders:new"]
        await stream.aclose()

    async def test_a_busy_feed_still_sleeps_between_polls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Other tables' entries are not this table's changes: a caught-up poll sleeps."""
        server = FakeChangeFeedServer()
        server.writes_per_query = 3
        feed = _feed(server, poll_interval=0.5)
        sleeps: list[int] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(len(server.queries))
            if len(sleeps) == 3:
                feed.stop()

        monkeypatch.setattr(change_feed_module.asyncio, "sleep", fake_sleep)
        assert [entry async for entry in feed.stream(since=0)] == []
        assert sleeps == [1, 2, 3]

    async def test_default_start_finds_the_end_of_a_long_feed(self) -> None:
        """More than one server page (1000 entries) before "now"."""
        server = FakeChangeFeedServer()
        for i in range(2500):
            server.write("other" if i % 3 else "orders", {"id": f"x:{i}"})
        feed = _feed(server, poll_interval=0.01)
        stream = feed.stream()
        first = asyncio.ensure_future(anext(stream))
        await _wait_cursor(feed)
        assert feed.cursor == str((server.next_version - 1) * STEP + 1)
        assert len(server.queries) < 100
        server.write("orders", {"id": "orders:new"})
        entry = await asyncio.wait_for(first, 2.0)
        assert _ids([entry]) == ["orders:new"]
        await stream.aclose()

    async def test_each_change_is_delivered_once_and_the_cursor_is_past_it(self) -> None:
        """The cursor used to be the last versionstamp, and ``SINCE`` includes it."""
        server = FakeChangeFeedServer()
        a = server.write("orders", {"id": "orders:a"})
        b = server.write("orders", {"id": "orders:b"})
        feed = _feed(server, poll_interval=0.01)
        stream = feed.stream(since=0)
        entries = await _take(stream, 2)
        assert _ids(entries) == ["orders:a", "orders:b"]
        assert [e["versionstamp"] for e in entries] == [a, b]
        assert feed.cursor == str(b + 1)
        server.write("orders", {"id": "orders:c"})
        assert _ids(await _take(stream, 1)) == ["orders:c"]
        await stream.aclose()

    async def test_stream_since_cursor_resumes_after_the_last_delivered_change(self) -> None:
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:a"})
        first = _feed(server, poll_interval=0.01)
        stream = first.stream(since=0)
        await _take(stream, 1)
        await stream.aclose()
        server.write("orders", {"id": "orders:b"})
        second = _feed(server, poll_interval=0.01).stream(since=first.cursor)
        assert _ids(await _take(second, 1)) == ["orders:b"]
        await second.aclose()

    async def test_stream_reaches_a_change_behind_a_page_of_other_tables(self) -> None:
        """At least ``batch_size`` foreign entries after the cursor used to stall the stream."""
        server = FakeChangeFeedServer()
        feed = _feed(server, poll_interval=0.01, batch_size=5)
        stream = feed.stream(since=0)
        for i in range(23):
            server.write("other", {"id": f"other:{i}"})
        server.write("orders", {"id": "orders:late"})
        assert _ids(await _take(stream, 1)) == ["orders:late"]
        await stream.aclose()

    async def test_a_version_cut_by_the_limit_is_read_again(self) -> None:
        """Entries sharing a commit version sort by table name; ``LIMIT`` can split them."""
        server = FakeChangeFeedServer()
        for i in range(999):
            server.write("aaa", {"id": f"aaa:{i}"}, version=10 + i)
        server.write("aaa", {"id": "aaa:last"}, version=1009)
        server.write("orders", {"id": "orders:1009"}, version=1009)
        server.write("orders", {"id": "orders:1010"}, version=1010)
        stream = _feed(server, poll_interval=0.01).stream(since=0)
        assert _ids(await _take(stream, 2)) == ["orders:1009", "orders:1010"]
        await stream.aclose()

    async def test_an_idle_poll_sleeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No poll follows an empty one without a sleep, and a delivered entry is not repeated."""
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:a"})
        feed = _feed(server, poll_interval=0.5)
        polls_before_sleep: list[int] = []

        async def fake_sleep(delay: float) -> None:
            polls_before_sleep.append(len(server.queries))
            if len(polls_before_sleep) == 3:
                feed.stop()

        monkeypatch.setattr(change_feed_module.asyncio, "sleep", fake_sleep)
        delivered = [entry async for entry in feed.stream(since=0)]
        assert _ids(delivered) == ["orders:a"]
        # Delivery poll, then an empty poll before each sleep: one query per poll.
        assert polls_before_sleep == [2, 3, 4]

    async def test_stream_raises_instead_of_retrying_a_rejected_statement(self) -> None:
        server = FakeChangeFeedServer()
        server.statement_error = "Parse error"
        with pytest.raises(ChangeFeedError):
            await asyncio.wait_for(anext(_feed(server, poll_interval=0.01).stream(since=0)), 2.0)
        assert len(server.queries) == 1

    async def test_stream_batch_raises_instead_of_retrying(self) -> None:
        server = FakeChangeFeedServer()
        server.fail_with = ConnectionError("connection reset")
        with pytest.raises(ChangeFeedError):
            await asyncio.wait_for(anext(_feed(server, poll_interval=0.01).stream_batch(since=0)), 2.0)
        assert len(server.queries) == 1

    async def test_stream_batch_yields_at_most_batch_size_and_advances(self) -> None:
        server = FakeChangeFeedServer()
        for i in range(7):
            server.write("orders", {"id": f"orders:{i}"})
        feed = _feed(server, poll_interval=0.01, batch_size=3)
        batches = feed.stream_batch(since=0)
        got = await _take(batches, 3)
        assert [len(batch) for batch in got] == [3, 3, 1]
        assert _ids([entry for batch in got for entry in batch]) == [f"orders:{i}" for i in range(7)]
        assert feed.cursor == str(got[-1][-1]["versionstamp"] + 1)
        await batches.aclose()


class TestMultiTableChangeFeedStream:
    """Each table keeps its own cursor, and errors are raised."""

    async def test_each_table_advances_its_own_cursor(self) -> None:
        server = FakeChangeFeedServer()
        server.write("users", {"id": "users:1"})
        server.write("orders", {"id": "orders:1"})
        feed = MultiTableChangeFeed(server, ["users", "orders"], poll_interval=0.01)  # type: ignore[arg-type]
        stream = feed.stream(since=0)
        got = await _take(stream, 2)
        assert sorted((table, _ids([entry])[0]) for table, entry in got) == [("orders", "orders:1"), ("users", "users:1")]
        server.write("orders", {"id": "orders:2"})
        table, entry = (await _take(stream, 1))[0]
        assert (table, _ids([entry])) == ("orders", ["orders:2"])
        await stream.aclose()

    async def test_since_is_honoured(self) -> None:
        server = FakeChangeFeedServer()
        server.write("users", {"id": "users:old"})
        cutoff = server.write("orders", {"id": "orders:old"}) + 1
        server.write("users", {"id": "users:new"})
        feed = MultiTableChangeFeed(server, ["users", "orders"], poll_interval=0.01)  # type: ignore[arg-type]
        stream = feed.stream(since=str(cutoff))
        assert [(t, _ids([e])) for t, e in await _take(stream, 1)] == [("users", ["users:new"])]
        await stream.aclose()

    async def test_default_start_while_other_tables_keep_writing(self) -> None:
        server = FakeChangeFeedServer()
        server.write("users", {"id": "users:old"})
        server.writes_per_query = 3
        feed = MultiTableChangeFeed(server, ["users", "orders"], poll_interval=0.01)  # type: ignore[arg-type]
        stream = feed.stream()
        first = asyncio.ensure_future(anext(stream))
        for _ in range(200):
            if len(server.queries) >= 2:
                break
            await asyncio.sleep(0.01)
        server.write("orders", {"id": "orders:new"})
        table, entry = await asyncio.wait_for(first, 2.0)
        assert (table, _ids([entry])) == ("orders", ["orders:new"])
        await stream.aclose()

    async def test_errors_are_raised(self) -> None:
        server = FakeChangeFeedServer()
        server.statement_error = "IAM error"
        feed = MultiTableChangeFeed(server, ["users", "orders"], poll_interval=0.01)  # type: ignore[arg-type]
        with pytest.raises(ChangeFeedError):
            await asyncio.wait_for(anext(feed.stream(since=0)), 2.0)


# ==================== ORM: ChangeModelStream ====================


class _FakeFeed:
    """Stands in for ``ChangeFeedStream``: moves its cursor past an entry before yielding it."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries
        self.cursor: str | None = "10"

    async def stream(self, since: Any = None) -> Any:
        for entry in self._entries:
            self.cursor = str(entry["versionstamp"] + 1)
            yield entry

    def stop(self) -> None:
        pass


class TestChangeModelStream:
    def test_parse_change_include_original_update(self) -> None:
        """``CHANGEFEED ... INCLUDE ORIGINAL``: the record is under ``current``, ``update`` is a JSON patch."""
        stream = ChangeModelStream(model=I308Player, connection=None, table="i308_players")
        change = {
            "versionstamp": 7 * STEP,
            "changes": [
                {
                    "current": {"id": RecordId("i308_players", "abc"), "name": "Bob", "age": 31},
                    "update": [{"op": "replace", "path": "/age", "value": 30}],
                },
            ],
        }
        events = stream._parse_change(change)
        assert [(e.action, e.record_id, e.instance.name, e.instance.age) for e in events] == [
            (LiveAction.UPDATE, "i308_players:abc", "Bob", 31)
        ]

    def test_parse_change_create_arrives_as_update(self) -> None:
        stream = ChangeModelStream(model=I308Player, connection=None, table="i308_players")
        change = {"versionstamp": STEP, "changes": [{"update": {"id": RecordId("i308_players", "new"), "name": "Neo"}}]}
        events = stream._parse_change(change)
        assert [(e.action, e.record_id, e.instance.name) for e in events] == [(LiveAction.UPDATE, "i308_players:new", "Neo")]

    async def test_cursor_stays_on_an_entry_until_its_last_event(self) -> None:
        """A checkpoint taken mid-entry must not skip the entry's remaining events."""
        stream = ChangeModelStream(model=I308Player, connection=None, table="i308_players")
        stream._feed = _FakeFeed(  # type: ignore[assignment]
            [
                {"versionstamp": 2 * STEP, "changes": [{"update": {"id": f"i308_players:{k}"}} for k in "abc"]},
                {"versionstamp": 3 * STEP, "changes": [{"update": {"id": "i308_players:d"}}]},
            ]
        )
        seen = [(event.record_id, stream.cursor) async for event in stream]
        assert seen == [
            ("i308_players:a", str(2 * STEP)),
            ("i308_players:b", str(2 * STEP)),
            ("i308_players:c", str(2 * STEP + 1)),
            ("i308_players:d", str(3 * STEP + 1)),
        ]

    async def test_cursor_after_an_entry_without_model_events_is_the_feeds(self) -> None:
        stream = ChangeModelStream(model=I308Player, connection=None, table="i308_players")
        stream._feed = _FakeFeed(  # type: ignore[assignment]
            [
                {"versionstamp": 2 * STEP, "changes": [{"define_table": {"name": "i308_players"}}]},
                {"versionstamp": 3 * STEP, "changes": [{"update": {"id": "i308_players:d"}}]},
            ]
        )
        events = [(event.record_id, stream.cursor) async for event in stream]
        assert events == [("i308_players:d", str(3 * STEP + 1))]
