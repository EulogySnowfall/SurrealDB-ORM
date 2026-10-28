"""Tests for streaming modules (Change Feeds and Live Queries)."""

import asyncio
import re
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.exceptions import ChangeFeedError, QueryError
from src.surreal_sdk.protocol.cbor import RecordId
from src.surreal_sdk.streaming import change_feed as change_feed_module
from src.surreal_sdk.streaming.change_feed import ChangeFeedStream, MultiTableChangeFeed, _render_since, _window_units
from src.surreal_sdk.streaming.live_query import LiveAction, LiveNotification, LiveQuery, LiveQueryManager
from src.surreal_sdk.types import QueryResponse


class TestChangeFeedStream:
    """Tests for ChangeFeedStream class."""

    def test_init(self) -> None:
        """Test initialization."""
        mock_conn = MagicMock()
        stream = ChangeFeedStream(mock_conn, "orders")

        assert stream.table == "orders"
        assert stream.poll_interval == 0.1
        assert stream.batch_size == 100
        assert stream.cursor is None

    def test_custom_settings(self) -> None:
        """Test custom settings."""
        mock_conn = MagicMock()
        stream = ChangeFeedStream(
            mock_conn,
            "orders",
            poll_interval=1.0,
            batch_size=50,
        )

        assert stream.poll_interval == 1.0
        assert stream.batch_size == 50


class TestLiveNotification:
    """Tests for LiveNotification class."""

    def test_from_dict_create(self) -> None:
        """Test parsing CREATE notification."""
        data = {"id": "live-query-uuid", "action": "CREATE", "result": {"id": "users:1", "name": "Alice"}}
        notification = LiveNotification.from_dict(data)

        assert notification.id == "live-query-uuid"
        assert notification.action == LiveAction.CREATE
        assert notification.result == {"id": "users:1", "name": "Alice"}

    def test_from_dict_update(self) -> None:
        """Test parsing UPDATE notification."""
        data = {"id": "live-query-uuid", "action": "UPDATE", "result": {"id": "users:1", "name": "Alice Updated"}}
        notification = LiveNotification.from_dict(data)

        assert notification.action == LiveAction.UPDATE

    def test_from_dict_delete(self) -> None:
        """Test parsing DELETE notification."""
        data = {"id": "live-query-uuid", "action": "DELETE", "result": {"id": "users:1"}}
        notification = LiveNotification.from_dict(data)

        assert notification.action == LiveAction.DELETE


class TestLiveAction:
    """Tests for LiveAction enum."""

    def test_action_values(self) -> None:
        """Test action enum values."""
        assert LiveAction.CREATE == "CREATE"
        assert LiveAction.UPDATE == "UPDATE"
        assert LiveAction.DELETE == "DELETE"


class TestLiveQuery:
    """Tests for LiveQuery class."""

    def test_init(self) -> None:
        """Test initialization."""
        mock_conn = MagicMock()
        live = LiveQuery(mock_conn, "users")

        assert live.table == "users"
        assert live.where is None
        assert live.diff is False
        assert not live.is_active
        assert live.live_id is None

    def test_init_with_options(self) -> None:
        """Test initialization with options."""
        mock_conn = MagicMock()
        live = LiveQuery(
            mock_conn,
            "users",
            where="age > 21",
            diff=True,
        )

        assert live.where == "age > 21"
        assert live.diff is True


class TestLiveQueryManager:
    """Tests for LiveQueryManager class."""

    def test_init(self) -> None:
        """Test initialization."""
        mock_conn = MagicMock()
        manager = LiveQueryManager(mock_conn)

        assert manager.count == 0
        assert manager.active_queries == []


class TestMultiTableChangeFeed:
    """Tests for MultiTableChangeFeed class."""

    def test_init(self) -> None:
        """Test initialization."""
        mock_conn = MagicMock()
        feed = MultiTableChangeFeed(mock_conn, ["users", "orders", "products"])

        assert len(feed.streams) == 3
        assert "users" in feed.streams
        assert "orders" in feed.streams
        assert "products" in feed.streams


# ==================== Change feed: SINCE rendering (#281) ====================


class TestRenderSince:
    """``SINCE`` takes a datetime literal or an unquoted versionstamp, never a quoted string."""

    def test_aware_datetime_becomes_utc_datetime_literal(self) -> None:
        since = datetime(2026, 10, 27, 14, 30, 5, 123456, tzinfo=timezone(timedelta(hours=2)))
        assert _render_since(since) == "d'2026-10-27T12:30:05.123456Z'"

    def test_utc_datetime_has_a_single_zone_designator(self) -> None:
        """``isoformat() + "Z"`` used to give ``…+00:00Z``."""
        assert _render_since(datetime(2026, 10, 27, 12, tzinfo=UTC)) == "d'2026-10-27T12:00:00.000000Z'"

    def test_naive_datetime_is_read_as_utc_with_a_warning(self) -> None:
        with pytest.warns(RuntimeWarning, match="read as UTC"):
            rendered = _render_since(datetime(2026, 10, 27, 12, 0, 0))
        assert rendered == "d'2026-10-27T12:00:00.000000Z'"

    def test_iso_string_with_zone_designator(self) -> None:
        assert _render_since("2026-10-27T12:00:00Z") == "d'2026-10-27T12:00:00.000000Z'"

    def test_iso_string_with_offset_is_converted_to_utc(self) -> None:
        assert _render_since("2026-10-27T12:00:00-04:00") == "d'2026-10-27T16:00:00.000000Z'"

    def test_iso_string_without_offset_is_read_as_utc(self) -> None:
        assert _render_since("2026-10-27T12:00:00") == "d'2026-10-27T12:00:00.000000Z'"

    def test_date_only_string_is_midnight_utc(self) -> None:
        """The example in the ``QuerySet.changes()`` docstring."""
        assert _render_since("2026-01-01") == "d'2026-01-01T00:00:00.000000Z'"

    def test_numeric_string_is_an_unquoted_versionstamp(self) -> None:
        assert _render_since("117515092609728513") == "117515092609728513"

    def test_int_is_an_unquoted_versionstamp(self) -> None:
        assert _render_since(0) == "0"
        assert _render_since(117515092609728513) == "117515092609728513"

    def test_largest_versionstamp_is_accepted(self) -> None:
        assert _render_since(2**64 - 1) == str(2**64 - 1)

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


# ==================== Change feed: a model of the server (#281) ====================


_SHOW_RE = re.compile(r"SHOW CHANGES FOR (?:TABLE (\w+)|DATABASE) SINCE (\S+)(?: LIMIT (\d+))?")


class FakeChangeFeedServer:
    """In-memory ``SHOW CHANGES``, with the semantics measured on SurrealDB 3.3.4.

    The change feed is one ordered list of ``(versionstamp, table)`` keys for
    the whole database. ``LIMIT`` counts those keys *before* the table filter
    (100 without a ``LIMIT``, never more than 1000), and the kept keys that
    share a versionstamp are merged into one entry. ``SINCE`` accepts an
    unquoted versionstamp or a ``d'…'`` literal compared as nanoseconds since
    the epoch (so a realistic date selects nothing, as on 3.3.4 in memory); a
    quoted string is a parse error.
    """

    def __init__(self) -> None:
        self.keys: list[tuple[int, str, list[dict[str, Any]]]] = []
        self.queries: list[str] = []
        self.next_vs = 1000
        self.fail_with: Exception | None = None
        self.statement_error: str | None = None
        #: A stream that polls without making progress fails here instead of hanging the test.
        self.max_queries = 500

    def write(self, table: str, *records: dict[str, Any], vs: int | None = None) -> int:
        """Record one transaction on *table* and return its versionstamp."""
        if vs is None:
            vs = self.next_vs
            self.next_vs += 1
        self.keys.append((vs, table, [{"update": record} for record in records] or [{"update": {}}]))
        self.keys.sort(key=lambda key: (key[0], key[1]))
        return vs

    def show(self, table: str | None, since: str, limit: int | None) -> list[dict[str, Any]]:
        if since.startswith("'"):
            raise QueryError("Parse error: Unexpected token `a strand`, expected a version stamp or a date-time")
        if since.startswith("d'"):
            moment = datetime.fromisoformat(since[2:-1])
            start = int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1000
        else:
            start = int(since)
        scanned = [key for key in self.keys if key[0] >= start][: min(limit or 100, 1000)]
        entries: list[dict[str, Any]] = []
        for vs, tb, changes in scanned:
            if table is not None and tb != table:
                continue
            if entries and entries[-1]["versionstamp"] == vs:
                entries[-1]["changes"].extend(changes)
            else:
                entries.append({"versionstamp": vs, "changes": list(changes)})
        return entries

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        self.queries.append(sql)
        if len(self.queries) > self.max_queries:
            raise AssertionError("too many SHOW CHANGES queries: the stream is spinning")
        if self.fail_with is not None:
            raise self.fail_with
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

    @property
    def show_queries(self) -> int:
        return len(self.queries)


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


class TestChangeFeedQueries:
    """The statements sent, the cursor and paging past other tables' entries."""

    async def test_invalid_since_sends_no_query(self) -> None:
        server = FakeChangeFeedServer()
        feed = _feed(server)
        with pytest.raises(ValueError):
            await feed.get_changes(since="yesterday")
        with pytest.raises(ValueError):
            await anext(feed.stream(since="yesterday"))
        with pytest.raises(ValueError):
            await anext(feed.stream_batch(since="not a date"))
        assert server.queries == []

    async def test_get_changes_since_zero_returns_the_table_entries(self) -> None:
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:1"})
        server.write("other", {"id": "other:1"})
        server.write("orders", {"id": "orders:2"})
        entries = await _feed(server).get_changes(since=0)
        assert _ids(entries) == ["orders:1", "orders:2"]
        assert all("'" not in query.replace("d'", "") for query in server.queries)

    async def test_get_changes_pages_past_other_tables(self) -> None:
        """``LIMIT`` counts every table's entries, so one page can hold none of ours."""
        server = FakeChangeFeedServer()
        for i in range(250):
            server.write("other", {"id": f"other:{i}"})
        server.write("orders", {"id": "orders:1"})
        entries = await _feed(server, batch_size=10).get_changes(since=0)
        assert _ids(entries) == ["orders:1"]

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

    async def test_stream_default_start_skips_existing_entries(self) -> None:
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:old"})
        feed = _feed(server, poll_interval=0.01)
        stream = feed.stream()
        first = asyncio.ensure_future(anext(stream))
        for _ in range(100):
            if feed.cursor is not None:
                break
            await asyncio.sleep(0.01)
        server.write("orders", {"id": "orders:new"})
        entry = await asyncio.wait_for(first, 2.0)
        assert _ids([entry]) == ["orders:new"]
        await stream.aclose()

    async def test_default_start_finds_the_end_of_a_long_feed(self) -> None:
        """More than one server page (1000 keys) before "now"."""
        server = FakeChangeFeedServer()
        for i in range(2500):
            server.write("other" if i % 3 else "orders", {"id": f"x:{i}"})
        feed = _feed(server, poll_interval=0.01)
        stream = feed.stream()
        first = asyncio.ensure_future(anext(stream))
        for _ in range(200):
            if feed.cursor is not None:
                break
            await asyncio.sleep(0.01)
        assert feed.cursor == str(server.next_vs)
        assert server.show_queries < 100
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

    async def test_a_versionstamp_cut_by_the_limit_is_read_again(self) -> None:
        """Keys sharing a versionstamp sort by table name; LIMIT can split them."""
        server = FakeChangeFeedServer()
        server.write("aaa", {"id": "aaa:0"}, vs=10)
        server.write("aaa", {"id": "aaa:1"}, vs=11)
        server.write("aaa", {"id": "aaa:2"}, vs=12)
        server.write("orders", {"id": "orders:12"}, vs=12)
        server.write("orders", {"id": "orders:13"}, vs=13)
        stream = _feed(server, poll_interval=0.01, batch_size=3).stream(since=0)
        assert _ids(await _take(stream, 2)) == ["orders:12", "orders:13"]
        await stream.aclose()

    async def test_one_large_transaction_does_not_stall_the_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A versionstamp holding more changes than ``batch_size`` is passed, not polled forever."""
        server = FakeChangeFeedServer()
        server.write("other", *({"id": f"other:{i}"} for i in range(50)), vs=10)
        feed = _feed(server, poll_interval=0.5, batch_size=5)
        cursors_at_sleep: list[str | None] = []

        async def fake_sleep(delay: float) -> None:
            cursors_at_sleep.append(feed.cursor)
            feed.stop()

        monkeypatch.setattr(change_feed_module.asyncio, "sleep", fake_sleep)
        assert [entry async for entry in feed.stream(since=0)] == []
        assert cursors_at_sleep == ["11"]
        # The window ends the feed, so no second, empty read is needed (#346).
        assert server.show_queries == 1

    async def test_an_idle_poll_sleeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No poll follows an empty one without a sleep, and a delivered entry is not repeated."""
        server = FakeChangeFeedServer()
        server.write("orders", {"id": "orders:a"})
        feed = _feed(server, poll_interval=0.5)
        polls_before_sleep: list[int] = []

        async def fake_sleep(delay: float) -> None:
            polls_before_sleep.append(server.show_queries)
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
        assert server.show_queries == 1

    async def test_stream_batch_raises_instead_of_retrying(self) -> None:
        server = FakeChangeFeedServer()
        server.fail_with = ConnectionError("connection reset")
        with pytest.raises(ChangeFeedError):
            await asyncio.wait_for(anext(_feed(server, poll_interval=0.01).stream_batch(since=0)), 2.0)
        assert server.show_queries == 1

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

    def test_batch_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            ChangeFeedStream(MagicMock(), "orders", batch_size=0)


class TestMultiTableChangeFeedStream:
    """Each table keeps its own cursor (#281)."""

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

    async def test_errors_are_raised(self) -> None:
        server = FakeChangeFeedServer()
        server.statement_error = "IAM error"
        feed = MultiTableChangeFeed(server, ["users", "orders"], poll_interval=0.01)  # type: ignore[arg-type]
        with pytest.raises(ChangeFeedError):
            await asyncio.wait_for(anext(feed.stream(since=0)), 2.0)


# ==================== Change feed under a busy writer (#346) ====================


class BusyChangeFeedServer(FakeChangeFeedServer):
    """A server another table is written to before every query, so the feed is never empty."""

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        self.write("busy", {"id": f"busy:{self.next_vs}"})
        return await super().query(sql, vars)


def _dt(name: str) -> dict[str, Any]:
    return {"define_table": {"name": name, "changefeed": {"expiry": "1h", "original": False}}}


class TestWindowUnits:
    """``_window_units`` bounds the ``(versionstamp, table)`` units ``LIMIT`` counted (3.3.4 shapes)."""

    def test_a_merged_multi_table_entry_counts_each_table(self) -> None:
        entry = {
            "versionstamp": 5,
            "changes": [{"update": {"id": "sa:5"}}, {"update": {"id": "sb:4"}}, {"update": {"id": "sb:5"}}],
        }
        assert _window_units([entry]) == 2

    def test_define_table_shares_the_unit_of_its_records(self) -> None:
        entry = {
            "versionstamp": 7,
            "changes": [_dt("pc"), {"update": {"id": "pc:1"}}, _dt("pd"), {"update": {"id": "pd:1"}}],
        }
        assert _window_units([entry]) == 2
        assert _window_units([{"versionstamp": 1, "changes": [_dt("qa")]}, {"versionstamp": 2, "changes": [_dt("qb")]}]) == 2

    def test_a_record_id_object_is_read_by_its_table(self) -> None:
        entry = {
            "versionstamp": 5,
            "changes": [
                {"update": {"id": RecordId("sa", 5)}},
                {"update": {"id": RecordId("sb", 4)}},
                {"delete": {"id": "sb:5"}},
            ],
        }
        assert _window_units([entry]) == 2

    def test_include_original_and_delete_shapes(self) -> None:
        entry = {
            "versionstamp": 9,
            "changes": [
                {"current": {"id": "pb:1", "x": 2}, "update": [{"op": "replace", "path": "/x", "value": 1}]},
                {"delete": {"id": "pb:2", "original": {"id": "pb:2"}}},
                {"delete": {"id": "pa:2"}},
            ],
        }
        assert _window_units([entry]) == 2

    def test_a_change_without_a_table_counts_on_its_own(self) -> None:
        entry = {
            "versionstamp": 3,
            "changes": [{"update": {}}, {"update": {"id": 42}}, {"something_new": {}}, {"update": {"id": "sa:1"}}],
        }
        assert _window_units([entry]) == 4

    def test_an_entry_counts_at_least_one(self) -> None:
        assert _window_units([{"versionstamp": 1, "changes": []}, {"versionstamp": 2}]) == 2
        assert _window_units([]) == 0


class TestBusyWriter:
    """Writes on another table between reads used to keep the readers going forever."""

    async def test_resolve_now_returns(self) -> None:
        server = BusyChangeFeedServer()
        for i in range(30):
            server.write("other" if i % 2 else "orders", {"id": f"x:{i}"})
        now = await asyncio.wait_for(_feed(server)._resolve_now(), 2.0)
        assert now >= 1030
        assert server.show_queries == 1

    async def test_get_changes_returns_the_table_entries(self) -> None:
        server = BusyChangeFeedServer()
        server.write("orders", {"id": "orders:1"})
        server.write("other", {"id": "other:1"})
        server.write("orders", {"id": "orders:2"})
        entries = await asyncio.wait_for(_feed(server).get_changes(since=0), 2.0)
        assert _ids(entries) == ["orders:1", "orders:2"]

    async def test_get_changes_pages_past_other_tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(change_feed_module, "_SCAN_LIMIT", 4)
        server = BusyChangeFeedServer()
        for i in range(20):
            server.write("orders" if i % 5 == 0 else "other", {"id": f"{'orders' if i % 5 == 0 else 'other'}:{i}"})
        entries = await asyncio.wait_for(_feed(server).get_changes(since=0), 2.0)
        assert _ids(entries) == ["orders:0", "orders:5", "orders:10", "orders:15"]

    async def test_stream_default_start_delivers_a_later_change(self) -> None:
        server = BusyChangeFeedServer()
        server.write("orders", {"id": "orders:old"})
        feed = _feed(server, poll_interval=0.01)
        stream = feed.stream()
        first = asyncio.ensure_future(anext(stream))
        for _ in range(100):
            if feed.cursor is not None:
                break
            await asyncio.sleep(0.01)
        assert feed.cursor is not None
        server.write("orders", {"id": "orders:new"})
        entry = await asyncio.wait_for(first, 2.0)
        assert _ids([entry]) == ["orders:new"]
        await stream.aclose()

    async def test_multi_table_default_start_delivers_a_later_change(self) -> None:
        server = BusyChangeFeedServer()
        server.write("users", {"id": "users:old"})
        feed = MultiTableChangeFeed(server, ["users", "orders"], poll_interval=0.01)  # type: ignore[arg-type]
        stream = feed.stream()
        first = asyncio.ensure_future(anext(stream))
        for _ in range(100):
            if all(s.cursor is not None for s in feed.streams.values()):
                break
            await asyncio.sleep(0.01)
        server.write("orders", {"id": "orders:new"})
        table, entry = await asyncio.wait_for(first, 2.0)
        assert (table, _ids([entry])) == ("orders", ["orders:new"])
        await stream.aclose()

    async def test_a_caught_up_poll_sleeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Other tables' writes are no reason to poll again without ``poll_interval``."""
        server = BusyChangeFeedServer()
        server.write("orders", {"id": "orders:a"})
        feed = _feed(server, poll_interval=0.5)
        sleeps: list[tuple[float, int]] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append((delay, server.show_queries))
            if len(sleeps) == 3:
                feed.stop()

        monkeypatch.setattr(change_feed_module.asyncio, "sleep", fake_sleep)
        delivered = [entry async for entry in feed.stream(since=0)]
        assert _ids(delivered) == ["orders:a"]
        # Delivery poll, then one caught-up poll before each sleep.
        assert sleeps == [(0.5, 2), (0.5, 3), (0.5, 4)]

    async def test_a_caught_up_multi_table_poll_sleeps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        server = BusyChangeFeedServer()
        feed = MultiTableChangeFeed(server, ["users", "orders"], poll_interval=0.5)  # type: ignore[arg-type]
        sleeps: list[tuple[float, int]] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append((delay, server.show_queries))
            if len(sleeps) == 3:
                feed.stop()

        monkeypatch.setattr(change_feed_module.asyncio, "sleep", fake_sleep)
        assert [item async for item in feed.stream(since=0)] == []
        # One read per table before each sleep.
        assert sleeps == [(0.5, 2), (0.5, 4), (0.5, 6)]


class TestWindowCutMidVersion:
    """On 3.3 a window shorter than ``LIMIT`` can end inside a multi-table versionstamp."""

    @staticmethod
    def _server() -> FakeChangeFeedServer:
        # Keys (10, aaa), (10, bbb), (11, aaa), (11, orders): LIMIT 3 returns two
        # entries, the second holding only ``aaa`` of versionstamp 11.
        server = FakeChangeFeedServer()
        server.write("aaa", {"id": "aaa:0"}, vs=10)
        server.write("bbb", {"id": "bbb:0"}, vs=10)
        server.write("aaa", {"id": "aaa:1"}, vs=11)
        server.write("orders", {"id": "orders:11"}, vs=11)
        return server

    async def test_the_window_is_cut_inside_a_versionstamp(self) -> None:
        window = self._server().show(None, "0", 3)
        assert [e["versionstamp"] for e in window] == [10, 11]
        assert _ids(window) == ["aaa:0", "bbb:0", "aaa:1"]

    async def test_get_changes_delivers_the_entry_at_the_cut(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(change_feed_module, "_SCAN_LIMIT", 3)
        entries = await asyncio.wait_for(_feed(self._server()).get_changes(since=0), 2.0)
        assert _ids(entries) == ["orders:11"]

    async def test_stream_delivers_the_entry_at_the_cut(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(change_feed_module, "_SCAN_LIMIT", 3)
        stream = _feed(self._server(), poll_interval=0.01).stream(since=0)
        assert _ids(await _take(stream, 1)) == ["orders:11"]
        await stream.aclose()

    async def test_counting_entries_would_lose_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The criterion of 2.x (entries < LIMIT), which this fake must defeat."""
        monkeypatch.setattr(change_feed_module, "_SCAN_LIMIT", 3)
        monkeypatch.setattr(change_feed_module, "_window_units", len)
        assert await _feed(self._server()).get_changes(since=0) == []


class TestStreamingIntegration:
    """Integration tests requiring a running SurrealDB instance."""

    @pytest.fixture(scope="function")
    async def http_connection(self) -> AsyncGenerator[HTTPConnection, None]:
        """Create a connected HTTP connection."""
        conn = HTTPConnection("http://localhost:8000", "test", "test")
        try:
            await conn.connect()
            await conn.signin("root", "root")
            # SurrealDB 3.0: ensure ns/db exist
            await conn.query("DEFINE NAMESPACE IF NOT EXISTS `test`; DEFINE DATABASE IF NOT EXISTS `test`;")
            yield conn
        finally:
            await conn.close()

    @pytest.fixture(scope="function")
    async def ws_connection(self) -> AsyncGenerator[WebSocketConnection, None]:
        """Create a connected WebSocket connection."""
        conn = WebSocketConnection(
            "ws://localhost:8000",
            "test",
            "test",
            auto_reconnect=False,
        )
        try:
            await conn.connect()
            await conn.signin("root", "root")
            # SurrealDB 3.0: ensure ns/db exist
            await conn.query("DEFINE NAMESPACE IF NOT EXISTS `test`; DEFINE DATABASE IF NOT EXISTS `test`;")
            yield conn
        finally:
            await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_define_changefeed(self, http_connection: HTTPConnection) -> None:
        """Test defining a change feed."""
        stream = ChangeFeedStream(http_connection, "cf_test")

        # This may fail if already defined, which is OK
        try:
            await stream.define_changefeed("1h")
        except Exception:
            pass

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_live_query_subscription(self, ws_connection: WebSocketConnection) -> None:
        """Test live query subscription flow."""
        notifications: list[LiveNotification] = []

        async def callback(notification: LiveNotification) -> None:
            notifications.append(notification)

        # SurrealDB 3.0: table must exist for LIVE SELECT
        await ws_connection.query("DEFINE TABLE IF NOT EXISTS lq_test;")

        live = LiveQuery(ws_connection, "lq_test")

        # Subscribe
        live_id = await live.subscribe(callback)
        assert live.is_active
        assert live_id is not None

        # Unsubscribe
        await live.unsubscribe()
        assert not live.is_active

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_live_query_manager(self, ws_connection: WebSocketConnection) -> None:
        """Test live query manager."""

        async def dummy_callback(notification: Any) -> None:
            pass

        # SurrealDB 3.0: tables must exist for LIVE SELECT
        await ws_connection.query("DEFINE TABLE IF NOT EXISTS lqm_test1; DEFINE TABLE IF NOT EXISTS lqm_test2;")

        manager = LiveQueryManager(ws_connection)

        # Watch multiple tables
        id1 = await manager.watch("lqm_test1", dummy_callback)
        id2 = await manager.watch("lqm_test2", dummy_callback)

        assert manager.count == 2
        assert id1 in manager.active_queries
        assert id2 in manager.active_queries

        # Unwatch all
        await manager.unwatch_all()
        assert manager.count == 0
