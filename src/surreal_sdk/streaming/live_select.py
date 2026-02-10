"""
Live Select Stream Implementation.

Provides async iterator pattern for real-time change notifications via WebSocket Live Queries.
"""

from typing import Any, Callable, Awaitable, Coroutine, Self
from dataclasses import dataclass, field
from enum import StrEnum
import asyncio

from ..connection.websocket import WebSocketConnection
from ..exceptions import LiveQueryError


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
        """Get the live query UUID."""
        return self._live_id

    @staticmethod
    def _format_value(value: Any) -> str:
        """Format a Python value for inline substitution in SurrealQL.

        SurrealDB LIVE SELECT does not evaluate session variables ($param)
        in WHERE clauses, so parameters must be inlined directly into the
        query string.
        """
        if value is None:
            return "NONE"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            # Escape single quotes inside the string
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            return f"'{escaped}'"
        if isinstance(value, (list, tuple)):
            items = ", ".join(LiveSelectStream._format_value(v) for v in value)
            return f"[{items}]"
        # Fallback: convert to string and quote
        escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"

    @staticmethod
    def _inline_params_static(sql: str, params: dict[str, Any]) -> str:
        """Replace $param references with inline values in the SQL string."""
        result = sql
        # Sort by key length descending to avoid partial replacements
        # (e.g. $_f10 should be replaced before $_f1)
        for key in sorted(params, key=len, reverse=True):
            result = result.replace(f"${key}", LiveSelectStream._format_value(params[key]))
        return result

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
                        self.connection._register_live_subscription(
                            self._live_id,
                            LiveSubscriptionParams(
                                table=self.table,
                                where=self.where,
                                params=self.params,
                                diff=self.diff,
                                callback=None,  # We use queue, not callback
                                on_reconnect=self.on_reconnect,
                            ),
                        )

                    self._active = True
                    return self._live_id

            raise LiveQueryError("No live query ID returned")

        except Exception as e:
            raise LiveQueryError(f"Failed to start live select: {e}")

    async def stop(self) -> None:
        """Stop the live query subscription."""
        if self._live_id:
            try:
                # Unregister from auto-resubscribe
                self.connection._unregister_live_subscription(self._live_id)
                await self.connection.kill(self._live_id)
            except Exception:
                pass
            finally:
                self.connection._live_callbacks.pop(self._live_id, None)
                self._live_id = None
                self._active = False

        self._closed = True
        # Signal end of stream
        await self._queue.put(None)  # type: ignore

    async def _handle_notification(self, data: dict[str, Any]) -> None:
        """Handle incoming live query notification."""
        change = LiveChange.from_dict(data)
        await self._queue.put(change)

    def _update_live_id(self, new_id: str) -> None:
        """Update live ID after reconnection (called by connection)."""
        self._live_id = new_id
        # Re-register callback with new ID
        self.connection._live_callbacks[new_id] = self._handle_notification

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
        asyncio.create_task(self._forward_to_callback(stream, callback))

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
