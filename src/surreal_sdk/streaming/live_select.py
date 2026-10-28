"""
Live Select Stream Implementation.

Provides async iterator pattern for real-time change notifications via WebSocket Live Queries.
"""

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Self
from uuid import UUID

from ..connection.websocket import WebSocketConnection
from ..exceptions import LiveQueryError
from ..protocol.cbor import RecordId, format_decimal

_SAFE_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\Z")

#: A ``$name`` parameter reference, read the way SurrealDB's lexer reads it.
_PARAM_REF_RE = re.compile(r"\$([A-Za-z0-9_]+)", re.ASCII)


def _string_literal(text: str) -> str:
    """Write *text* as a single-quoted SurrealQL string holding exactly *text*.

    Inside the quotes a backslash starts a two-character sequence, so each
    backslash is written as two backslashes and each single quote as a
    backslash followed by the quote. Every other character stands for itself.
    """
    body = str.__str__(text).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{body}'"


def _backtick_name(name: str) -> str:
    """Write a table name or string record id between backticks.

    Inside backticks SurrealQL reads two backslashes as one backslash, and a
    backslash followed by a backtick as one backtick (checked on SurrealDB
    2.7.0). A doubled backtick is not read as one, and a lone trailing
    backslash would take the closing backtick with it, so each backslash and
    each backtick of *name* is written that way.
    """
    body = str.__str__(name).replace("\\", "\\\\").replace("`", "\\`")
    return f"`{body}`"


def _record_literal(value: RecordId) -> str:
    """Write the record link that designates exactly the record *value* binds to.

    The table and a string id always go between backticks, so a keyword, a
    leading digit or any other character keeps its meaning: the string id
    ``"123"`` is ``t:`123```, a different record from the numeric ``t:123``.
    An ``int`` id stays a bare number; an array, object or UUID id is written
    as that value's literal.
    """
    if not isinstance(value.table, str):
        raise TypeError(f"live() filter: record table must be a str, got {type(value.table).__name__}")
    record_id = value.id
    if isinstance(record_id, bool):
        raise TypeError("live() filter: a record id cannot be a bool")
    if isinstance(record_id, int):
        id_text = int.__repr__(record_id)
    elif isinstance(record_id, str):
        id_text = _backtick_name(record_id)
    elif isinstance(record_id, (list, tuple, dict, UUID)):
        id_text = LiveSelectStream._format_value(record_id)
    else:
        raise TypeError(f"live() filter: record ids of type {type(record_id).__name__} have no SurrealQL literal")
    return f"{_backtick_name(value.table)}:{id_text}"


class LiveAction(StrEnum):
    """Live query action types."""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


@dataclass
class LiveChange:
    """
    Enhanced live query change notification.

    Attributes:
        id: Live query UUID
        action: CREATE, UPDATE, or DELETE
        record_id: The affected record ID (e.g., "players:abc")
        result: The full record after the change
        before: The record before the change (if DIFF mode)
        changed_fields: List of changed field names (if DIFF mode)
    """

    id: str
    action: LiveAction
    record_id: str
    result: dict[str, Any]
    before: dict[str, Any] | None = None
    changed_fields: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveChange":
        """Parse from WebSocket message."""
        result = data.get("result", {})
        record_id = ""

        # Extract record ID from result
        if isinstance(result, dict) and "id" in result:
            record_id = str(result["id"])

        # Parse changed fields if present (DIFF mode)
        changed_fields: list[str] = []
        if isinstance(result, list):
            # DIFF mode returns list of patches
            for patch in result:
                if isinstance(patch, dict) and "path" in patch:
                    # JSON Patch format: {"op": "replace", "path": "/field", "value": ...}
                    path = patch.get("path", "")
                    if path.startswith("/"):
                        changed_fields.append(path[1:].split("/")[0])

        return cls(
            id=str(data.get("id", "")),
            action=LiveAction(data.get("action", "UPDATE")),
            record_id=record_id,
            result=result if isinstance(result, dict) else {},
            before=None,  # Could be populated from DIFF data
            changed_fields=changed_fields,
        )


# Type alias for callbacks
LiveCallback = Callable[[LiveChange], Awaitable[None]]
ReconnectCallback = Callable[[str, str], Coroutine[Any, Any, None]]  # (old_id, new_id)


@dataclass
class LiveSubscriptionParams:
    """Parameters for recreating a live subscription after reconnect."""

    table: str
    where: str | None
    params: dict[str, Any]
    diff: bool
    callback: LiveCallback | None
    on_reconnect: ReconnectCallback | None
    # Private: set by LiveSelectStream, which receives notifications through its
    # own handler rather than ``callback``. The connection calls it with the id
    # of the re-created live query as soon as it has it, with no await in
    # between, so the stream re-registers its handler and adopts the new id. It
    # returns False when the stream was stopped while its query was being
    # re-created. Not a constructor argument, and left out of repr and equality.
    _on_resubscribed: Callable[[str], bool] | None = field(default=None, init=False, repr=False, compare=False)


class LiveSelectStream:
    """
    Async iterator for live query subscriptions.

    Provides a pythonic async iterator interface for receiving real-time
    database change notifications.

    Usage:
        async with conn.live_select("players", where="table_id = $id", params={"id": table_id}) as stream:
            async for change in stream:
                match change.action:
                    case LiveAction.CREATE:
                        print(f"New player: {change.result}")
                    case LiveAction.UPDATE:
                        print(f"Player updated: {change.record_id}")
                    case LiveAction.DELETE:
                        print(f"Player left: {change.record_id}")
    """

    def __init__(
        self,
        connection: WebSocketConnection,
        table: str,
        where: str | None = None,
        params: dict[str, Any] | None = None,
        diff: bool = False,
        auto_resubscribe: bool = True,
        on_reconnect: ReconnectCallback | None = None,
    ):
        """
        Initialize live select stream.

        Args:
            connection: WebSocket connection to use
            table: Table to watch
            where: Optional WHERE clause filter (e.g., "table_id = $id")
            params: Parameters for the WHERE clause
            diff: If True, receive only changed fields
            auto_resubscribe: If True, automatically resubscribe on reconnect
            on_reconnect: Optional callback when resubscribed (old_id, new_id)
        """
        self.connection = connection
        self.table = table
        if not _SAFE_TABLE_RE.fullmatch(table):
            raise ValueError(f"Invalid table name: {table!r}")
        self.where = where
        self.params = params or {}
        self.diff = diff
        self.auto_resubscribe = auto_resubscribe
        self.on_reconnect = on_reconnect

        self._live_id: str | None = None
        self._queue: asyncio.Queue[LiveChange] = asyncio.Queue()
        self._active = False
        self._closed = False

    @property
    def is_active(self) -> bool:
        """Check if stream is active."""
        return self._active and self._live_id is not None

    @property
    def live_id(self) -> str | None:
        """Get the live query UUID (after an auto-resubscribe, the re-created query's)."""
        return self._live_id

    @staticmethod
    def _format_value(value: Any) -> str:
        """Render a Python value as the SurrealQL literal of the same value.

        SurrealDB 2.x does not evaluate bound parameters (``$param``) in a
        ``LIVE SELECT`` WHERE clause, so the values are written into the query
        text. Each literal designates exactly the value the CBOR encoder sends
        when the same Python value is bound as a parameter (checked against
        SurrealDB 2.7.0), so a ``live()`` filter matches what ``exec()`` matches:

        - ``None`` → ``NONE``; ``bool`` → ``true`` / ``false``; ``int`` → its digits
        - ``float`` → its ``repr`` (finite values only)
        - ``Decimal`` → positional digits with the ``dec`` suffix, e.g. ``1.50dec``,
          written by :func:`~surreal_sdk.protocol.cbor.format_decimal` like a
          bound ``Decimal`` (finite and in range only; places beyond the 28th
          rounded off as SurrealDB rounds them)
        - ``str`` → a single-quoted string with the same characters
        - ``UUID`` → ``u'...'``
        - ``datetime`` → ``d'...'`` in UTC; a naive datetime is read as UTC
        - ``date`` → ``d'YYYY-MM-DD'``, midnight UTC
        - ``list`` / ``tuple`` → an array; ``dict`` with ``str`` keys → an object
        - ``RecordId`` → the record link: the table and a string id between
          backticks, an ``int`` id as a bare number

        Args:
            value: The value to render

        Returns:
            The SurrealQL literal

        Raises:
            TypeError: The value is of any other type (``set``, ``bytes``,
                ``Duration``, ``Table``, arbitrary objects...), is a NaN or
                infinite ``float``/``Decimal``, or holds a dict key that is not
                a ``str`` or a record id of an unsupported type.
            ValueError: A ``Decimal`` is above the largest SurrealDB decimal
                in magnitude.
        """
        if value is None:
            return "NONE"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, int):
            return int.__repr__(value)
        if isinstance(value, float):
            if not math.isfinite(value):
                raise TypeError(f"live() filter values must be finite numbers, got {value!r}")
            return float.__repr__(value)
        if isinstance(value, Decimal):
            if not value.is_finite():
                raise TypeError(f"live() filter values must be finite numbers, got {value!r}")
            return f"{format_decimal(value)}dec"
        if isinstance(value, str):
            return _string_literal(value)
        if isinstance(value, UUID):
            return f"u'{value}'"
        if isinstance(value, datetime):
            if value.utcoffset() is None:
                value = value.replace(tzinfo=UTC)
            return f"d'{value.astimezone(UTC).isoformat()}'"
        if isinstance(value, date):
            return f"d'{value.isoformat()}'"
        if isinstance(value, RecordId):
            return _record_literal(value)
        if isinstance(value, (list, tuple)):
            items = ", ".join(LiveSelectStream._format_value(v) for v in value)
            return f"[{items}]"
        if isinstance(value, dict):
            entries: list[str] = []
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"live() filter: object keys must be str, got key {key!r}")
                entries.append(f"{_string_literal(key)}: {LiveSelectStream._format_value(item)}")
            return "{" + ", ".join(entries) + "}"
        raise TypeError(f"live() filter values of type {type(value).__name__} have no SurrealQL literal: {value!r}")

    @staticmethod
    def _inline_params_static(sql: str, params: dict[str, Any]) -> str:
        """Replace each ``$name`` that has a value in *params* with its literal.

        One pass with a callable replacement: the inserted text is never
        scanned again, so a ``$other`` inside a string value stays text, and
        it is never read as a ``re`` replacement pattern, so backslashes come
        through as rendered. A ``$name`` with no value in *params* (``$auth``,
        ``$this``...) is left as written.

        Args:
            sql: The query text
            params: The values for the ``$name`` references

        Returns:
            The query text with the values written in

        Raises:
            TypeError: A referenced value has no SurrealQL literal
                (see :meth:`_format_value`).
            ValueError: A referenced ``Decimal`` is out of range
                (see :meth:`_format_value`).
        """

        def render(match: re.Match[str]) -> str:
            name = match.group(1)
            if name in params:
                return LiveSelectStream._format_value(params[name])
            return match.group(0)

        return _PARAM_REF_RE.sub(render, sql)

    async def start(self) -> str:
        """
        Start the live query subscription.

        Returns:
            Live query UUID

        Raises:
            LiveQueryError: If subscription fails
        """
        if self._active:
            raise LiveQueryError("Live select already active")

        # Build query with variable substitution
        sql = f"LIVE SELECT * FROM {self.table}"
        if self.where:
            sql += f" WHERE {self.where}"
        if self.diff:
            sql += " DIFF"

        # SurrealDB LIVE SELECT does not evaluate session variables set via
        # LET in the WHERE clause.  Inline them directly into the SQL.
        if self.params:
            sql = self._inline_params_static(sql, self.params)

        try:
            response = await self.connection.query(sql)

            # Extract live query UUID
            if response.results:
                first_result = response.results[0]
                if first_result.is_ok:
                    result_data = first_result.result
                    if isinstance(result_data, str):
                        self._live_id = result_data
                    elif isinstance(result_data, dict) and "result" in result_data:
                        self._live_id = str(result_data["result"])
                    elif hasattr(result_data, "__str__"):
                        # Handle UUID objects from CBOR decoding
                        self._live_id = str(result_data)
                    else:
                        raise LiveQueryError("Invalid live query response")

                    # Register callback with connection
                    self.connection._live_callbacks[self._live_id] = self._handle_notification

                    # Register for auto-resubscribe if enabled
                    if self.auto_resubscribe:
                        subscription = LiveSubscriptionParams(
                            table=self.table,
                            where=self.where,
                            params=self.params,
                            diff=self.diff,
                            callback=None,  # We use queue, not callback
                            on_reconnect=self.on_reconnect,
                        )
                        # After a reconnect, the connection moves this stream to
                        # the re-created live query (handler and id).
                        subscription._on_resubscribed = self._update_live_id
                        self.connection._register_live_subscription(self._live_id, subscription)

                    self._active = True
                    return self._live_id

            raise LiveQueryError("No live query ID returned")

        except Exception as e:
            raise LiveQueryError(f"Failed to start live select: {e}")

    async def stop(self) -> None:
        """Stop the live query subscription."""
        # Marked stopped before the first await. A resubscribe whose re-created
        # LIVE SELECT is answered while the kill below is in flight must find
        # the stream stopped: ``_update_live_id`` then refuses the new id and
        # the connection kills that query. Marked only after the kill, the
        # stream adopted it while this kill reached the old id, which had
        # ended with the socket, and the new query kept running.
        self._closed = True
        live_id = self._live_id
        if live_id:
            try:
                # Unregister from auto-resubscribe
                self.connection._unregister_live_subscription(live_id)
                await self.connection.kill(live_id)
            except Exception:
                pass
            finally:
                self.connection._live_callbacks.pop(live_id, None)
                self._live_id = None
                self._active = False

        # Signal end of stream
        await self._queue.put(None)  # type: ignore

    async def _handle_notification(self, data: dict[str, Any]) -> None:
        """Handle incoming live query notification."""
        change = LiveChange.from_dict(data)
        await self._queue.put(change)

    def _update_live_id(self, new_id: str) -> bool:
        """
        Adopt the live query the connection re-created after a reconnect.

        Called by ``WebSocketConnection._resubscribe_one()`` as soon as the new
        id is known, with no await in between: the stream's handler is
        registered under the new id (the resubscribe cleared every handler),
        and ``stop()`` then kills the new live query instead of the old one,
        which ended with the socket.

        Returns:
            False once ``stop()`` has begun, even with its kill of the old id
            still in flight: the stream does not take the new id, and the
            connection kills that live query instead of keeping it.
        """
        if self._closed:
            return False
        self._live_id = new_id
        self.connection._live_callbacks[new_id] = self._handle_notification
        return True

    # Async iterator protocol

    def __aiter__(self) -> Self:
        """Return async iterator."""
        return self

    async def __anext__(self) -> LiveChange:
        """Get next change from stream."""
        if self._closed and self._queue.empty():
            raise StopAsyncIteration

        change = await self._queue.get()
        if change is None:
            raise StopAsyncIteration
        return change

    # Context manager protocol

    async def __aenter__(self) -> Self:
        """Start stream on context entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Stop stream on context exit."""
        await self.stop()


class LiveSelectManager:
    """
    Manage multiple live select streams on a single connection.

    Usage:
        manager = LiveSelectManager(conn)

        await manager.watch("game_tables", on_table_change, where="id = $id", params={"id": table_id})
        await manager.watch("players", on_player_change, where="table_id = $id", params={"id": table_id})

        # Keep running...
        await asyncio.sleep(3600)

        await manager.stop_all()
    """

    def __init__(self, connection: WebSocketConnection):
        """Initialize manager."""
        self.connection = connection
        self._streams: dict[str, LiveSelectStream] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    async def watch(
        self,
        table: str,
        callback: LiveCallback,
        where: str | None = None,
        params: dict[str, Any] | None = None,
        diff: bool = False,
        auto_resubscribe: bool = True,
        on_reconnect: ReconnectCallback | None = None,
    ) -> str:
        """
        Start watching a table with callback.

        Args:
            table: Table to watch
            callback: Async callback for changes
            where: Optional filter
            params: Filter parameters
            diff: If True, receive only changed fields
            auto_resubscribe: Resubscribe on reconnect
            on_reconnect: Callback when resubscribed

        Returns:
            Live query UUID
        """
        stream = LiveSelectStream(
            self.connection,
            table,
            where=where,
            params=params,
            diff=diff,
            auto_resubscribe=auto_resubscribe,
            on_reconnect=on_reconnect,
        )
        live_id = await stream.start()
        self._streams[live_id] = stream

        # Start background task to forward to callback
        task = asyncio.create_task(self._forward_to_callback(stream, callback))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        return live_id

    async def _forward_to_callback(self, stream: LiveSelectStream, callback: LiveCallback) -> None:
        """Forward stream changes to callback."""
        try:
            async for change in stream:
                await callback(change)
        except Exception:
            pass

    async def stop(self, live_id: str) -> None:
        """Stop a specific stream."""
        if live_id in self._streams:
            await self._streams[live_id].stop()
            del self._streams[live_id]

    async def stop_all(self) -> None:
        """Stop all streams."""
        for stream in list(self._streams.values()):
            await stream.stop()
        self._streams.clear()

    @property
    def active_streams(self) -> list[str]:
        """Get list of active stream IDs."""
        return list(self._streams.keys())

    @property
    def count(self) -> int:
        """Number of active streams."""
        return len(self._streams)
