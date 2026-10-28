"""
Change Feed Streaming Implementation.

Provides CDC (Change Data Capture) streaming via SurrealDB Change Feeds.
This is stateless and ideal for microservices architectures.

How the server reads the feed (measured on SurrealDB 3.3.4 and 2.7.0):

- ``SINCE`` takes an unquoted versionstamp or a ``d'…'`` datetime literal; a
  quoted string is a parse error.
- ``SINCE <n>`` includes the entry whose versionstamp is ``n``, so a cursor is
  the last versionstamp read plus one.
- The feed is one ordered list for the whole database. ``LIMIT`` counts the
  entries of every table *before* ``FOR TABLE`` keeps the ones of the table
  (100 without a ``LIMIT``, never more than 1000). An empty page therefore does
  not mean the table has caught up, so every poll reads the same window for the
  table and for the whole database, in one transaction, and advances the
  cursor past everything the window scanned.
- On 3.3, ``LIMIT`` counts ``(versionstamp, table)`` units while the database
  view merges the units of one versionstamp into one entry, so a window with
  fewer entries than ``LIMIT`` can still end in the middle of a versionstamp
  (see :func:`_window_units`). A window is the end of the feed only when its
  units are certainly fewer than ``LIMIT``.
- A ``CREATE`` is recorded as an ``update``.
"""

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from ..connection.http import HTTPConnection
from ..exceptions import ChangeFeedError
from ..protocol.cbor import RecordId, _warn_naive_datetime

_SAFE_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\Z")
_VERSIONSTAMP_RE = re.compile(r"[0-9]+")
_MAX_VERSIONSTAMP = 2**64 - 1

#: Entries the server scans per ``SHOW CHANGES`` (its own ceiling).
_SCAN_LIMIT = 1000


def _render_since(since: object) -> str:
    """Render a change feed start as a SurrealQL ``SINCE`` operand.

    Args:
        since: A ``datetime`` (a naive one is read as UTC, with the SDK's
            ``RuntimeWarning``), an ``int`` versionstamp, or a string holding
            either an ISO 8601 date or date-time (``"2026-01-01"``,
            ``"2026-10-27T12:00:00Z"``; no offset means UTC) or a decimal
            versionstamp (what :attr:`ChangeFeedStream.cursor` returns).

    Returns:
        ``d'<UTC ISO 8601>'`` or an unquoted versionstamp. The given text is
        never interpolated: the result is rebuilt from a parsed value.

    Raises:
        ValueError: The string is neither form, or the versionstamp is outside
            ``0 .. 2**64 - 1``.
        TypeError: Any other type (``bool`` included).
    """
    if isinstance(since, bool):
        raise TypeError(f"since must be a datetime, an int or a str, not {type(since).__name__}")
    if isinstance(since, int):
        if not 0 <= since <= _MAX_VERSIONSTAMP:
            raise ValueError(f"A versionstamp is between 0 and {_MAX_VERSIONSTAMP}, got {since}")
        return str(since)
    if isinstance(since, datetime):
        if since.tzinfo is None:
            _warn_naive_datetime()
            moment = since.replace(tzinfo=UTC)
        else:
            moment = since.astimezone(UTC)
        return "d'" + moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ") + "'"
    if isinstance(since, str):
        if _VERSIONSTAMP_RE.fullmatch(since):
            return _render_since(int(since))
        try:
            parsed = datetime.fromisoformat(since)
        except ValueError:
            raise ValueError(f"since must be an ISO 8601 date or date-time, or a decimal versionstamp, got {since!r}") from None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return _render_since(parsed)
    raise TypeError(f"since must be a datetime, an int or a str, not {type(since).__name__}")


def _versionstamp(entry: dict[str, Any]) -> int:
    value = entry.get("versionstamp")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChangeFeedError(f"Change feed entry without an integer versionstamp: {entry!r}")
    return value


def _record_table(record: object) -> str | None:
    """The table of a changed record, from its ``id``; ``None`` when unknown."""
    if not isinstance(record, dict):
        return None
    record_id = record.get("id")
    if isinstance(record_id, RecordId):
        return record_id.table if isinstance(record_id.table, str) else None
    if isinstance(record_id, str) and ":" in record_id:
        return record_id.split(":", 1)[0]
    return None


def _change_table(change: object) -> str | None:
    """The table one change belongs to, or ``None`` when it cannot be told."""
    if not isinstance(change, dict):
        return None
    define = change.get("define_table")
    if isinstance(define, dict):
        name = define.get("name")
        return name if isinstance(name, str) else None
    for key in ("update", "delete", "current"):
        table = _record_table(change.get(key))
        if table is not None:
            return table
    return None


def _window_units(window: list[dict[str, Any]]) -> int:
    """An upper bound on the ``(versionstamp, table)`` units the server counted for *window*.

    Measured on SurrealDB 3.3.4: ``SHOW CHANGES FOR DATABASE … LIMIT n``
    counts ``(versionstamp, table)`` units, and the database view merges the
    units of one versionstamp into a single entry. A transaction writing
    ``sa:5``, ``sb:4`` and ``sb:5`` is one entry and two units, and a ``LIMIT``
    can stop after its ``sa`` unit: the window then holds fewer entries than
    ``LIMIT`` and still ends in the middle of a versionstamp. (On 2.7.0 every
    unit is its own entry, which is why ``v2`` can count entries.)

    Each entry counts the distinct tables among its changes, and at least one.
    The table comes from the ``id`` of an ``update``, ``delete`` or
    ``current`` record (a decoded ``RecordId`` or a ``"table:id"`` string)
    and from the ``name`` of a ``define_table``, which shares the unit of the
    table's records. A change whose table cannot be told counts as a unit of
    its own, and two changes are only merged when their table names are equal,
    so the count errs upwards: a window wrongly counted as full costs one more
    read, never a lost entry. A unit with no visible change would be counted
    low; none has been observed, and the floor of one per entry bounds it.

    Args:
        window: Entries of ``SHOW CHANGES FOR DATABASE``.

    Returns:
        The number of units, at least the number of entries.
    """
    units = 0
    for entry in window:
        changes = entry.get("changes")
        tables: set[str] = set()
        unknown = 0
        if isinstance(changes, list):
            for change in changes:
                table = _change_table(change)
                if table is None:
                    unknown += 1
                else:
                    tables.add(table)
        units += max(1, len(tables) + unknown)
    return units


def _statement_error(response: Any) -> str | None:
    """The most useful error message among the statements of *response*."""
    errors = [str(result.result) for result in response.results if not result.is_ok]
    if not errors:
        return None
    # In a failed transaction every statement reports "not executed"; the cause is elsewhere.
    causes = [error for error in errors if "not executed" not in error and "Cannot COMMIT" not in error]
    return (causes or errors)[0]


class ChangeFeedStream:
    """
    Stream changes from a SurrealDB table using Change Feeds.

    Change Feeds capture database modifications as a historic stream,
    allowing replay from a versionstamp or a point in time. This is ideal for:
    - Microservices event streaming
    - Data replication
    - Audit trails
    - Event sourcing

    Each entry is ``{"versionstamp": int, "changes": [...]}``, where a change
    is ``{"update": record}`` (a create is reported as an update),
    ``{"delete": {...}}``, ``{"define_table": {...}}``, or, on a table defined
    with ``CHANGEFEED … INCLUDE ORIGINAL``, ``{"current": record, "update":
    [<JSON patch>]}``. Each entry is delivered once.

    Usage:
        async with HTTPConnection("http://localhost:8000", "ns", "db") as conn:
            await conn.signin("root", "root")

            stream = ChangeFeedStream(conn, "orders")

            async for change in stream.stream():
                print(f"Change: {change['changes']}")
    """

    def __init__(
        self,
        connection: HTTPConnection,
        table: str,
        poll_interval: float = 0.1,
        batch_size: int = 100,
    ):
        """
        Initialize Change Feed stream.

        Args:
            connection: HTTP connection to use
            table: Table to stream changes from
            poll_interval: Seconds between polls when no changes
            batch_size: Maximum entries handed over per poll (and per batch
                of :meth:`stream_batch`)
        """
        self.connection = connection
        self.table = table
        if not _SAFE_TABLE_RE.fullmatch(table):
            raise ValueError(f"Invalid table name: {table!r}")
        if batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {batch_size}")
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._cursor: str | None = None
        self._running = False

    @property
    def cursor(self) -> str | None:
        """The next versionstamp to read, as a decimal string.

        It is one past the last entry delivered (or scanned), and can be passed
        back as ``since`` to resume a stream where this one stopped. ``None``
        until a stream has started.
        """
        return self._cursor

    async def define_changefeed(self, retention: str = "7d") -> None:
        """
        Define a change feed on the table.

        Must be called before streaming if not already defined.

        Args:
            retention: How long to keep changes (e.g., "1h", "7d", "30d")
        """
        try:
            await self.connection.query(f"DEFINE TABLE {self.table} CHANGEFEED {retention}")
        except Exception as e:
            raise ChangeFeedError(f"Failed to define change feed: {e}")

    async def _read(self, since: str, limit: int) -> tuple[list[dict[str, Any]], int | None, bool]:
        """Read one window of the feed.

        Args:
            since: A rendered ``SINCE`` operand.
            limit: Maximum number of this table's entries to return.

        Returns:
            The table's entries (at most *limit*), the cursor that follows
            them (``None`` when nothing at all was written after *since*), and
            whether the read reached the end of the feed. A window whose
            :func:`_window_units` are fewer than the scan limit was not cut by
            the server, so it ends the feed and the cursor passes its last
            versionstamp. The last versionstamp of a full window may have been
            cut by the server's ``LIMIT``, so it is read again unless it is the
            only one.

        Raises:
            ChangeFeedError: The request failed or a statement was rejected.
        """
        query = (
            f"BEGIN; SHOW CHANGES FOR TABLE {self.table} SINCE {since} LIMIT {_SCAN_LIMIT}; "
            f"SHOW CHANGES FOR DATABASE SINCE {since} LIMIT {_SCAN_LIMIT}; COMMIT;"
        )
        try:
            response = await self.connection.query(query)
        except Exception as e:
            raise ChangeFeedError(f"Failed to get changes for {self.table}: {e}") from e
        error = _statement_error(response)
        if error is not None:
            raise ChangeFeedError(f"Failed to get changes for {self.table}: {error}")
        shows = [result.result for result in response.results if isinstance(result.result, list)]
        if len(shows) != 2:
            raise ChangeFeedError(f"Unexpected SHOW CHANGES response for {self.table}: {response.results!r}")
        table_entries, window = shows
        if not window:
            return [], None, True

        last = _versionstamp(window[-1])
        caught_up = _window_units(window) < _SCAN_LIMIT
        complete = caught_up or _versionstamp(window[0]) == last
        boundary = last + 1 if complete else last
        entries = [entry for entry in table_entries if _versionstamp(entry) < boundary]
        if len(entries) > limit:
            entries = entries[:limit]
            boundary = _versionstamp(entries[-1]) + 1
            caught_up = False
        return entries, boundary, caught_up

    async def _resolve_now(self) -> int:
        """The versionstamp after the last entry of the feed, i.e. "now".

        A ``d'<now>'`` start would be simpler, but SurrealDB 3.3 (in-memory
        storage) compares it against versionstamps on another scale and selects
        nothing, so the feed is read once from its beginning instead. The cost
        is proportional to the retained feed of the whole database. It stops
        at the first window that ends the feed (see :func:`_window_units`),
        so writers on other tables cannot keep it reading.
        """
        cursor = 0
        while True:
            query = f"SHOW CHANGES FOR DATABASE SINCE {cursor} LIMIT {_SCAN_LIMIT};"
            try:
                response = await self.connection.query(query)
            except Exception as e:
                raise ChangeFeedError(f"Failed to read the change feed: {e}") from e
            error = _statement_error(response)
            if error is not None:
                raise ChangeFeedError(f"Failed to read the change feed: {error}")
            window = response.results[0].result if response.results else None
            if not isinstance(window, list) or not window:
                return cursor
            last = _versionstamp(window[-1])
            if _window_units(window) < _SCAN_LIMIT:
                return last + 1
            cursor = last + 1 if _versionstamp(window[0]) == last else last

    async def _windows(self, since: str | int | datetime | None) -> AsyncIterator[list[dict[str, Any]]]:
        """Poll the feed from *since*, yielding each non-empty batch of entries.

        :attr:`cursor` is the versionstamp after everything handed out, and
        after the other tables' entries the last window scanned. A poll that
        reaches the end of the feed without an entry for the table is followed
        by ``poll_interval`` of sleep, even while other tables are written.
        """
        operand = _render_since(since) if since is not None else str(await self._resolve_now())
        if not operand.startswith("d'"):
            self._cursor = operand
        self._running = True
        try:
            while self._running:
                entries, cursor, caught_up = await self._read(operand, self.batch_size)
                if cursor is None:
                    await asyncio.sleep(self.poll_interval)
                    continue
                operand = str(cursor)
                if entries:
                    yield entries
                self._cursor = operand
                if caught_up and not entries:
                    await asyncio.sleep(self.poll_interval)
        finally:
            self._running = False

    async def get_changes(
        self,
        since: str | int | datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get the table's changes since a specific point.

        Args:
            since: A versionstamp (``int`` or decimal string, inclusive), a
                ``datetime``, or an ISO 8601 date or date-time string (UTC when
                it has no offset). ``None`` reads from :attr:`cursor`, and
                returns nothing when no stream has set one.
            limit: Maximum number of entries to return (default
                ``batch_size``). Entries of other tables do not count.

        Returns:
            List of change entries, oldest first. It is a snapshot: reading
            stops at the end of the feed as it stood, so writes made meanwhile
            (on any table) may or may not be included.

        Raises:
            ValueError: *since* is not one of the accepted forms (no query is sent).
            ChangeFeedError: The request failed or the server rejected it.
        """
        if since is None:
            if self._cursor is None:
                return []
            since = self._cursor
        operand = _render_since(since)
        limit = limit or self.batch_size

        collected: list[dict[str, Any]] = []
        while len(collected) < limit:
            entries, cursor, caught_up = await self._read(operand, limit - len(collected))
            collected.extend(entries)
            if cursor is None or caught_up:
                break
            operand = str(cursor)
        return collected

    async def stream(
        self,
        since: str | int | datetime | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Stream changes continuously, each entry once.

        Args:
            since: Starting point, in any form :meth:`get_changes` accepts.
                ``None`` starts after the last entry already in the feed, so
                only changes written from now on are delivered.

        Yields:
            Change entries as they become available

        Raises:
            ValueError: *since* is not one of the accepted forms (no query is sent).
            ChangeFeedError: A poll failed or the server rejected it. The
                stream is not retried: a rejected statement never heals.
        """
        async for batch in self._windows(since):
            for entry in batch:
                self._cursor = str(_versionstamp(entry) + 1)
                yield entry

    def stop(self) -> None:
        """Stop the stream."""
        self._running = False

    async def stream_batch(
        self,
        since: str | int | datetime | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Stream changes in batches of at most ``batch_size`` entries.

        More efficient for high-volume scenarios.

        Args:
            since: Starting point, as for :meth:`stream`

        Yields:
            Batches of change entries

        Raises:
            ValueError: *since* is not one of the accepted forms (no query is sent).
            ChangeFeedError: A poll failed or the server rejected it.
        """
        async for batch in self._windows(since):
            self._cursor = str(_versionstamp(batch[-1]) + 1)
            yield batch


class MultiTableChangeFeed:
    """
    Stream changes from multiple tables.

    Useful for aggregating changes across related tables. Each table keeps its
    own cursor; the tables are polled in turn.

    Usage::

        from surreal_sdk.streaming.change_feed import MultiTableChangeFeed

        feed = MultiTableChangeFeed(conn, ["users", "orders"])
        async for table, change in feed.stream():
            print(table, change["versionstamp"])
    """

    def __init__(
        self,
        connection: HTTPConnection,
        tables: list[str],
        poll_interval: float = 0.1,
        batch_size: int = 100,
    ):
        """
        Initialize multi-table change feed.

        Args:
            connection: HTTP connection to use
            tables: List of tables to stream
            poll_interval: Seconds between polls when no table has changes
            batch_size: Maximum entries per table per poll
        """
        self.streams = {table: ChangeFeedStream(connection, table, poll_interval, batch_size) for table in tables}
        self.poll_interval = poll_interval
        self._running = False

    async def stream(
        self,
        since: str | int | datetime | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """
        Stream changes from all tables, one entry at a time.

        Args:
            since: Starting point for every table, in any form
                :meth:`ChangeFeedStream.get_changes` accepts; ``None`` means now.

        Yields:
            Tuple of (table_name, change entry)

        Raises:
            ValueError: *since* is not one of the accepted forms (no query is sent).
            ChangeFeedError: A poll failed or the server rejected it.
        """
        if not self.streams:
            return
        first = next(iter(self.streams.values()))
        start = _render_since(since) if since is not None else str(await first._resolve_now())
        operands = dict.fromkeys(self.streams, start)
        self._running = True

        try:
            while self._running:
                progressed = False
                for table, stream in self.streams.items():
                    entries, cursor, caught_up = await stream._read(operands[table], stream.batch_size)
                    if cursor is None:
                        continue
                    if entries or not caught_up:
                        progressed = True
                    operands[table] = str(cursor)
                    for entry in entries:
                        stream._cursor = str(_versionstamp(entry) + 1)
                        yield table, entry
                    stream._cursor = operands[table]
                if not progressed:
                    await asyncio.sleep(self.poll_interval)
        finally:
            self._running = False

    def stop(self) -> None:
        """Stop all streams."""
        self._running = False
        for stream in self.streams.values():
            stream.stop()
