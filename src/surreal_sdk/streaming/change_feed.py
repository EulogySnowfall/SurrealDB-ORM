"""
Change Feed Streaming Implementation.

Provides CDC (Change Data Capture) streaming via SurrealDB Change Feeds.
This is stateless and ideal for microservices architectures.
"""

from typing import Any, AsyncGenerator, AsyncIterator
from datetime import datetime
import asyncio

from ..connection.http import HTTPConnection
from ..exceptions import ChangeFeedError


class ChangeFeedStream:
    """
    Stream changes from a SurrealDB table using Change Feeds.

    Change Feeds capture database modifications as a historic stream,
    allowing replay from specific timestamps. This is ideal for:
    - Microservices event streaming
    - Data replication
    - Audit trails
    - Event sourcing

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
            batch_size: Maximum changes per poll
        """
        self.connection = connection
        self.table = table
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self._cursor: str | None = None
        self._running = False

    @property
    def cursor(self) -> str | None:
        """Current stream cursor (versionstamp or timestamp)."""
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

    async def get_changes(
        self,
        since: str | datetime | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Get changes since a specific point.

        Args:
            since: Timestamp or versionstamp to start from
            limit: Maximum number of changes to return

        Returns:
            List of change records
        """
        if since is None:
            since = self._cursor or datetime.utcnow().isoformat() + "Z"
        elif isinstance(since, datetime):
            since = since.isoformat() + "Z"

        limit = limit or self.batch_size

        query = f"SHOW CHANGES FOR TABLE {self.table} SINCE '{since}' LIMIT {limit}"

        try:
            response = await self.connection.query(query)

            if response.results:
                first_result = response.results[0]
                if first_result.is_ok:
                    result_data = first_result.result
                    if isinstance(result_data, list):
                        return result_data
                    elif isinstance(result_data, dict):
                        return [result_data]

            return []

        except Exception as e:
            raise ChangeFeedError(f"Failed to get changes: {e}")

    async def stream(
        self,
        since: str | datetime | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Stream changes continuously.

        Args:
            since: Starting point (timestamp or versionstamp)

        Yields:
            Change records as they become available
        """
        if since is None:
            since = datetime.utcnow().isoformat() + "Z"
        elif isinstance(since, datetime):
            since = since.isoformat() + "Z"

        self._cursor = since
        self._running = True

        while self._running:
            try:
                changes = await self.get_changes(since=self._cursor)

                for change in changes:
                    yield change

                    # Update cursor with versionstamp if available
                    versionstamp = change.get("versionstamp")
                    if versionstamp:
                        self._cursor = str(versionstamp)

                if not changes:
                    await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception:
                # Log error but continue streaming
                await asyncio.sleep(self.poll_interval)

    def stop(self) -> None:
        """Stop the stream."""
        self._running = False

    async def stream_batch(
        self,
        since: str | datetime | None = None,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """
        Stream changes in batches.

        More efficient for high-volume scenarios.

        Args:
            since: Starting point

        Yields:
            Batches of change records
        """
        if since is None:
            since = datetime.utcnow().isoformat() + "Z"
        elif isinstance(since, datetime):
            since = since.isoformat() + "Z"

        self._cursor = since
        self._running = True

        while self._running:
            try:
                changes = await self.get_changes(since=self._cursor)

                if changes:
                    yield changes

                    # Update cursor with last versionstamp
                    last_change = changes[-1]
                    versionstamp = last_change.get("versionstamp")
                    if versionstamp:
                        self._cursor = str(versionstamp)
                else:
                    await asyncio.sleep(self.poll_interval)

            except asyncio.CancelledError:
                self._running = False
                raise
            except Exception:
                await asyncio.sleep(self.poll_interval)


class MultiTableChangeFeed:
    """
    Stream changes from multiple tables.

    Useful for aggregating changes across related tables.
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
            poll_interval: Seconds between polls
            batch_size: Maximum changes per table per poll
        """
        self.streams = {table: ChangeFeedStream(connection, table, poll_interval, batch_size) for table in tables}
        self._running = False

    async def stream(
        self,
        since: str | datetime | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """
        Stream changes from all tables.

        Yields:
            Tuple of (table_name, change_record)
        """
        self._running = True

        async def stream_table(table: str, stream: ChangeFeedStream) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
            async for change in stream.stream(since):
                yield table, change

        # Create tasks for all tables
        async def merged_stream() -> None:
            # This is a simplified implementation
            # A production version would use asyncio.Queue
            for table, stream in self.streams.items():
                _ = (table, stream)  # Placeholder for future implementation

        # Simple round-robin implementation
        while self._running:
            for table, stream in self.streams.items():
                try:
                    changes = await stream.get_changes()
                    for change in changes:
                        yield table, change
                except Exception:
                    pass

            if not any(stream._cursor for stream in self.streams.values()):
                await asyncio.sleep(self.streams[list(self.streams.keys())[0]].poll_interval)

    def stop(self) -> None:
        """Stop all streams."""
        self._running = False
        for stream in self.streams.values():
            stream.stop()
