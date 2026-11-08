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
- ``SINCE <n> LIMIT 1`` returns the first entry at or after ``n`` (or part of
  it), which is all a probe needs: "now" is found by an exponential then
  binary search over the 64-bit versionstamps (:func:`_find_now`), at most
  :data:`_PROBE_BOUND` small requests whatever the size of the retained feed.
- A versionstamp on 3.3.5 is the commit time in milliseconds shifted left by
  16 bits, plus a counter. Whether a ``d'…'`` start is compared with it
  depends on the storage engine (measured on 3.3.5): RocksDB selects the
  entries written at or after that instant; in-memory storage and SurrealKV
  select every entry for ``d'1970-01-01T00:00:00Z'`` and none for any later
  instant, so a stream started there from a datetime never delivers. A stream
  warns once when that happens (:class:`_DatetimeStartCheck`); a versionstamp
  start works on every engine.
"""

import asyncio
import re
import warnings
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from ..connection.http import HTTPConnection
from ..exceptions import ChangeFeedError
from ..protocol.cbor import RecordId, _warn_naive_datetime
from ..transaction import _is_cancelled_statement_error

_SAFE_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\Z")
_VERSIONSTAMP_RE = re.compile(r"[0-9]+")
_MAX_VERSIONSTAMP = 2**64 - 1

#: Entries the server scans per ``SHOW CHANGES`` (its own ceiling).
_SCAN_LIMIT = 1000

#: Most requests :func:`_find_now` sends: the first probe, then at most 65
#: exponential and 64 binary steps over the 64-bit versionstamps.
_PROBE_BOUND = 1 + 65 + 64

#: How long after a datetime start a change must be written before missing it
#: counts as the engine ignoring the start (covers a client clock running ahead).
_CLOCK_MARGIN = timedelta(seconds=5)


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
    # In a failed transaction the other statements report "not executed" and the
    # COMMIT "the transaction was aborted"; the cause is elsewhere. A conflict
    # detected at commit time is reported on the COMMIT itself and is the cause.
    causes = [error for error in errors if not _is_cancelled_statement_error(error)]
    return (causes or errors)[0]


async def _probe(connection: HTTPConnection, since: int) -> int | None:
    """The versionstamp of the first entry at or after *since*, or ``None``.

    Raises:
        ChangeFeedError: The request failed or the statement was rejected.
    """
    try:
        response = await connection.query(f"SHOW CHANGES FOR DATABASE SINCE {since} LIMIT 1;")
    except Exception as e:
        raise ChangeFeedError(f"Failed to read the change feed: {e}") from e
    error = _statement_error(response)
    if error is not None:
        raise ChangeFeedError(f"Failed to read the change feed: {error}")
    window = response.results[0].result if response.results else None
    if not isinstance(window, list) or not window:
        return None
    return _versionstamp(window[0])


async def _find_now(connection: HTTPConnection) -> int:
    """The versionstamp after the last entry of the feed, i.e. "now".

    A ``d'<now>'`` start would be simpler, but in-memory storage and SurrealKV
    compare it on another scale and select nothing. The search sends
    ``SINCE <n> LIMIT 1`` probes and jumps past the versionstamp each one
    returns: first with a step doubling from 1, until a probe finds nothing,
    then by bisection between the last entry seen and that empty probe. That
    is at most :data:`_PROBE_BOUND` small requests, whatever the size of the
    retained feed, and gaps left by its retention do not matter. A probe
    reads the feed only from its start, so writers on any table cannot keep
    the search going: once a probe is empty, the bisection only narrows.

    The result is one past an entry that was read, and no later probe found
    anything from it on. A change committed while the search runs may or may
    not be counted as before "now"; a change committed after it returns is
    always after.

    Raises:
        ChangeFeedError: A probe failed or was rejected.
    """
    found = await _probe(connection, 0)
    if found is None:
        return 0
    low = found + 1  # "now" is at least one past an entry that exists
    step = 1
    high: int | None = None  # a probe here found nothing
    while high is None:
        if low > _MAX_VERSIONSTAMP:
            return low
        target = min(low - 1 + step, _MAX_VERSIONSTAMP)
        found = await _probe(connection, target)
        if found is None:
            high = target
        else:
            low = found + 1
            step *= 2
    while low < high:
        middle = (low + high) // 2
        found = await _probe(connection, middle)
        if found is None:
            high = middle
        else:
            # Past ``high`` only if a change was committed after that probe:
            # it is after "now" then, and so is everything following it.
            low = found + 1
    return low


async def _read_window(
    connection: HTTPConnection, tables: list[str], since: str
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Read the feed of each table and of the whole database over one window.

    Every statement has the same ``SINCE`` and ``LIMIT``, and ``LIMIT`` counts
    the entries of every table before ``FOR TABLE`` filters them, so all the
    statements scan the same keys, in one transaction.

    Args:
        connection: The connection to read on.
        tables: Validated table names.
        since: A rendered ``SINCE`` operand.

    Returns:
        Each table's entries, in the order of *tables*, and the database window.

    Raises:
        ChangeFeedError: The request failed or a statement was rejected.
    """
    reads = "".join(f"SHOW CHANGES FOR TABLE {table} SINCE {since} LIMIT {_SCAN_LIMIT}; " for table in tables)
    query = f"BEGIN; {reads}SHOW CHANGES FOR DATABASE SINCE {since} LIMIT {_SCAN_LIMIT}; COMMIT;"
    names = ", ".join(tables)
    try:
        response = await connection.query(query)
    except Exception as e:
        raise ChangeFeedError(f"Failed to get changes for {names}: {e}") from e
    error = _statement_error(response)
    if error is not None:
        raise ChangeFeedError(f"Failed to get changes for {names}: {error}")
    shows = [result.result for result in response.results if isinstance(result.result, list)]
    if len(shows) != len(tables) + 1:
        raise ChangeFeedError(f"Unexpected SHOW CHANGES response for {names}: {response.results!r}")
    return shows[:-1], shows[-1]


def _window_end(window: list[dict[str, Any]]) -> tuple[int, bool]:
    """Where a non-empty database window ends, and whether it ends the feed.

    A window whose :func:`_window_units` are fewer than the scan limit was not
    cut by the server: it ends the feed and the next read starts past its last
    versionstamp. The last versionstamp of a full window may have been cut by
    the server's ``LIMIT``, so it is read again, unless it is the only one.

    Returns:
        The cursor that follows the window, and whether it ends the feed.
    """
    last = _versionstamp(window[-1])
    caught_up = _window_units(window) < _SCAN_LIMIT
    complete = caught_up or _versionstamp(window[0]) == last
    return (last + 1 if complete else last), caught_up


def _split_entries(entries: list[dict[str, Any]], start: int, end: int, limit: int) -> tuple[list[dict[str, Any]], int, bool]:
    """A table's entries in ``start <= versionstamp < end``, at most *limit* of them.

    Returns:
        The entries, the cursor that follows them (*end*, or one past the last
        entry kept when *limit* cut them), and whether *limit* cut them.
    """
    kept = [entry for entry in entries if start <= _versionstamp(entry) < end]
    if len(kept) <= limit:
        return kept, end, False
    kept = kept[:limit]
    return kept, _versionstamp(kept[-1]) + 1, True


class _DatetimeStartCheck:
    """Warns once when a datetime start misses a change written after it.

    In-memory storage and SurrealKV select nothing for a ``d'…'`` start after
    the epoch (measured on 3.3.5), so such a stream waits forever without an
    error. An empty window alone proves nothing: on RocksDB a start after the
    last entry is empty too, until something is written. So once the start is
    :data:`_CLOCK_MARGIN` in the past and a window is empty, the check finds
    "now" (:func:`_find_now`); from then on, each empty poll is preceded by a
    probe from that point. A change found there was written after the start,
    and a window from the start that is still empty after it missed that
    change: the check warns, once, and the stream goes on as before. The
    first non-empty window ends the check, and so does the warning.
    """

    def __init__(self, connection: HTTPConnection, operand: str) -> None:
        self._connection = connection
        self._operand = operand
        self._moment = datetime.fromisoformat(operand[2:-1])
        self._active = True
        self._marker: int | None = None
        self._missed = False

    @property
    def active(self) -> bool:
        """Whether the check still runs (no non-empty window and no warning yet)."""
        return self._active

    async def before_read(self) -> None:
        """Probe for a change written after the marker, before the window is read."""
        if self._active and self._marker is not None:
            self._missed = await _probe(self._connection, self._marker) is not None

    async def after_read(self, empty: bool) -> None:
        """Account for the window just read; warn when it missed a later change."""
        if not self._active:
            return
        if not empty:
            self._active = False
        elif self._marker is None:
            if datetime.now(UTC) - _CLOCK_MARGIN >= self._moment:
                self._marker = await _find_now(self._connection)
        elif self._missed:
            self._active = False
            warnings.warn(
                f"The change feed start {self._operand} selected no change, although changes were written "
                "after it. SurrealDB compares a datetime start with the feed only on some storage engines "
                "(not in memory or on SurrealKV); start from a versionstamp instead (an int, or the "
                "cursor property of a stream).",
                RuntimeWarning,
                stacklevel=2,
            )


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
            whether the read reached the end of the feed (see
            :func:`_window_end`).

        Raises:
            ChangeFeedError: The request failed or a statement was rejected.
        """
        (table_entries,), window = await _read_window(self.connection, [self.table], since)
        if not window:
            return [], None, True
        end, caught_up = _window_end(window)
        entries, cursor, cut = _split_entries(table_entries, 0, end, limit)
        return entries, cursor, caught_up and not cut

    async def _resolve_now(self) -> int:
        """The versionstamp after the last entry of the feed, i.e. "now" (see :func:`_find_now`)."""
        return await _find_now(self.connection)

    async def _windows(self, since: str | int | datetime | None) -> AsyncIterator[list[dict[str, Any]]]:
        """Poll the feed from *since*, yielding each non-empty batch of entries.

        :attr:`cursor` is the versionstamp after everything handed out, and
        after the other tables' entries the last window scanned. A poll that
        reaches the end of the feed without an entry for the table is followed
        by ``poll_interval`` of sleep, even while other tables are written.
        A datetime start that misses a later change emits one
        ``RuntimeWarning`` (see :class:`_DatetimeStartCheck`).
        """
        operand = _render_since(since) if since is not None else str(await self._resolve_now())
        check: _DatetimeStartCheck | None = None
        if operand.startswith("d'"):
            check = _DatetimeStartCheck(self.connection, operand)
        else:
            self._cursor = operand
        self._running = True
        try:
            while self._running:
                if check is not None:
                    await check.before_read()
                entries, cursor, caught_up = await self._read(operand, self.batch_size)
                if check is not None:
                    await check.after_read(cursor is None)
                    if not check.active:
                        check = None
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
    own cursor. Each poll is one transaction: one ``SHOW CHANGES FOR TABLE``
    per table and a single ``SHOW CHANGES FOR DATABASE``, all from the
    smallest cursor, so they scan the same window; every table takes its
    entries from that window and advances its cursor past it.

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
                :meth:`ChangeFeedStream.get_changes` accepts; ``None`` means
                now. A datetime start that misses a later change emits one
                ``RuntimeWarning``, as :meth:`ChangeFeedStream.stream` does.

        Yields:
            Tuple of (table_name, change entry)

        Raises:
            ValueError: *since* is not one of the accepted forms (no query is sent).
            ChangeFeedError: A poll failed or the server rejected it.
        """
        if not self.streams:
            return
        first = next(iter(self.streams.values()))
        connection = first.connection
        tables = list(self.streams)
        start = _render_since(since) if since is not None else str(await first._resolve_now())
        check: _DatetimeStartCheck | None = None
        #: Each table's cursor; ``None`` while a datetime start has selected nothing.
        cursors: dict[str, int] | None = None
        if start.startswith("d'"):
            check = _DatetimeStartCheck(connection, start)
        else:
            cursors = dict.fromkeys(tables, int(start))
            for stream in self.streams.values():
                stream._cursor = start
        self._running = True

        try:
            while self._running:
                operand = start if cursors is None else str(min(cursors.values()))
                if check is not None:
                    await check.before_read()
                table_entries, window = await _read_window(connection, tables, operand)
                if check is not None:
                    await check.after_read(not window)
                    if not check.active:
                        check = None
                if not window:
                    await asyncio.sleep(self.poll_interval)
                    continue
                end, caught_up = _window_end(window)
                progressed = not caught_up
                if cursors is None:
                    cursors = dict.fromkeys(tables, 0)
                for table, entries in zip(tables, table_entries, strict=True):
                    stream = self.streams[table]
                    own = cursors[table]
                    batch, cursor, cut = _split_entries(entries, own, end, stream.batch_size)
                    cursors[table] = max(own, cursor)
                    if batch or cut:
                        progressed = True
                    for entry in batch:
                        stream._cursor = str(_versionstamp(entry) + 1)
                        yield table, entry
                    stream._cursor = str(cursors[table])
                if not progressed:
                    await asyncio.sleep(self.poll_interval)
        finally:
            self._running = False

    def stop(self) -> None:
        """Stop all streams."""
        self._running = False
        for stream in self.streams.values():
            stream.stop()
