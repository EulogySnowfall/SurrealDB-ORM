"""
Live Query Streaming Implementation.

Provides real-time change notifications via WebSocket Live Queries.
"""

from typing import Any, Callable, Awaitable
from dataclasses import dataclass
from enum import StrEnum

from ..connection.websocket import WebSocketConnection
from ..exceptions import LiveQueryError


class LiveAction(StrEnum):
    """Live query action types."""

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


@dataclass
class LiveNotification:
    """
    Live query notification.

    Attributes:
        id: Live query UUID
        action: CREATE, UPDATE, or DELETE
        result: The affected record
    """

    id: str
    action: LiveAction
    result: dict[str, Any]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LiveNotification":
        """Parse from WebSocket message."""
        return cls(
            id=data.get("id", ""),
            action=LiveAction(data.get("action", "UPDATE")),
            result=data.get("result", {}),
        )


# Type alias for callbacks
LiveCallback = Callable[[LiveNotification], Awaitable[None]]


class LiveQuery:
    """
    Manage a live query subscription.

    Live queries provide real-time notifications when data changes.
    They require a WebSocket connection.

    Usage:
        async with WebSocketConnection("ws://localhost:8000", "ns", "db") as conn:
            await conn.signin("root", "root")

            async def on_change(notification: LiveNotification):
                print(f"{notification.action}: {notification.result}")

            live = LiveQuery(conn, "orders")
            await live.subscribe(on_change)

            # Keep running...
            await asyncio.sleep(3600)

            await live.unsubscribe()
    """

    def __init__(
        self,
        connection: WebSocketConnection,
        table: str,
        where: str | None = None,
        diff: bool = False,
    ):
        """
        Initialize live query.

        Args:
            connection: WebSocket connection to use
            table: Table to watch
            where: Optional WHERE clause filter
            diff: If True, receive only changed fields
        """
        self.connection = connection
        self.table = table
        self.where = where
        self.diff = diff
        self._live_id: str | None = None
        self._callback: LiveCallback | None = None
        self._active = False

    @property
    def is_active(self) -> bool:
        """Check if live query is active."""
        return self._active and self._live_id is not None

    @property
    def live_id(self) -> str | None:
        """Get the live query UUID."""
        return self._live_id

    async def subscribe(self, callback: LiveCallback) -> str:
        """
        Start the live query subscription.

        Args:
            callback: Async function to call on changes

        Returns:
            Live query UUID

        Raises:
            LiveQueryError: If subscription fails
        """
        if self._active:
            raise LiveQueryError("Live query already active")

        self._callback = callback

        # Build query
        sql = f"LIVE SELECT * FROM {self.table}"
        if self.where:
            sql += f" WHERE {self.where}"
        if self.diff:
            sql += " DIFF"

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
                    else:
                        raise LiveQueryError("Invalid live query response")

                    # Register callback with connection
                    self.connection._live_callbacks[self._live_id] = self._handle_notification
                    self._active = True
                    return self._live_id

            raise LiveQueryError("No live query ID returned")

        except Exception as e:
            raise LiveQueryError(f"Failed to start live query: {e}")

    async def unsubscribe(self) -> None:
        """Stop the live query subscription."""
        if self._live_id:
            try:
                await self.connection.kill(self._live_id)
            except Exception:
                pass
            finally:
                self.connection._live_callbacks.pop(self._live_id, None)
                self._live_id = None
                self._active = False

    async def _handle_notification(self, data: dict[str, Any]) -> None:
        """Handle incoming live query notification."""
        if self._callback:
            notification = LiveNotification.from_dict(data)
            await self._callback(notification)

    async def __aenter__(self) -> "LiveQuery":
        """Context manager entry (requires subscribe call)."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        await self.unsubscribe()


class LiveQueryManager:
    """
    Manage multiple live queries on a single connection.

    Usage:
        async with WebSocketConnection("ws://localhost:8000", "ns", "db") as conn:
            await conn.signin("root", "root")

            manager = LiveQueryManager(conn)

            await manager.watch("users", on_user_change)
            await manager.watch("orders", on_order_change)
            await manager.watch("products", on_product_change)

            # Keep running...
            await asyncio.sleep(3600)

            await manager.unwatch_all()
    """

    def __init__(self, connection: WebSocketConnection):
        """
        Initialize live query manager.

        Args:
            connection: WebSocket connection to use
        """
        self.connection = connection
        self._queries: dict[str, LiveQuery] = {}

    async def watch(
        self,
        table: str,
        callback: LiveCallback,
        where: str | None = None,
        diff: bool = False,
    ) -> str:
        """
        Start watching a table.

        Args:
            table: Table to watch
            callback: Callback for changes
            where: Optional filter
            diff: If True, receive only changed fields

        Returns:
            Live query UUID
        """
        query = LiveQuery(self.connection, table, where, diff)
        live_id = await query.subscribe(callback)
        self._queries[live_id] = query
        return live_id

    async def unwatch(self, live_id: str) -> None:
        """
        Stop watching a specific live query.

        Args:
            live_id: Live query UUID to stop
        """
        if live_id in self._queries:
            await self._queries[live_id].unsubscribe()
            del self._queries[live_id]

    async def unwatch_all(self) -> None:
        """Stop all live queries."""
        for query in list(self._queries.values()):
            await query.unsubscribe()
        self._queries.clear()

    @property
    def active_queries(self) -> list[str]:
        """Get list of active live query IDs."""
        return list(self._queries.keys())

    @property
    def count(self) -> int:
        """Number of active live queries."""
        return len(self._queries)
