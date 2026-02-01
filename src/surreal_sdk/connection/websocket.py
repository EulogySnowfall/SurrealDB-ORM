"""
WebSocket Connection Implementation for SurrealDB SDK.

Provides stateful WebSocket-based connection for real-time features.
"""

from typing import TYPE_CHECKING, Any, Callable, Coroutine
import asyncio
import json

import aiohttp
from aiohttp import ClientWSTimeout

from .base import BaseSurrealConnection

if TYPE_CHECKING:
    from ..transaction import WebSocketTransaction
from ..protocol.rpc import RPCRequest, RPCResponse
from ..exceptions import ConnectionError, LiveQueryError, TimeoutError


# Type alias for live query callbacks
LiveCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class WebSocketConnection(BaseSurrealConnection):
    """
    WebSocket-based connection to SurrealDB.

    This connection is stateful - session is maintained across requests.
    Required for Live Queries and session variables.
    Ideal for real-time applications, dashboards, and collaborative features.
    """

    def __init__(
        self,
        url: str,
        namespace: str,
        database: str,
        timeout: float = 30.0,
        auto_reconnect: bool = True,
        reconnect_interval: float = 1.0,
        max_reconnect_attempts: int = 5,
    ):
        """
        Initialize WebSocket connection.

        Args:
            url: SurrealDB WebSocket URL (e.g., "ws://localhost:8000")
            namespace: Target namespace
            database: Target database
            timeout: Request timeout in seconds
            auto_reconnect: Whether to automatically reconnect on disconnect
            reconnect_interval: Seconds between reconnection attempts
            max_reconnect_attempts: Maximum reconnection attempts
        """
        # Normalize URL to WebSocket
        if url.startswith("http://"):
            url = url.replace("http://", "ws://", 1)
        elif url.startswith("https://"):
            url = url.replace("https://", "wss://", 1)

        # Ensure /rpc suffix
        if not url.endswith("/rpc"):
            url = url.rstrip("/") + "/rpc"

        super().__init__(url, namespace, database, timeout)

        self.auto_reconnect = auto_reconnect
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future[RPCResponse]] = {}
        self._live_callbacks: dict[str, LiveCallback] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._closing = False

    def _next_request_id(self) -> int:
        """Generate next request ID."""
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        """Establish WebSocket connection."""
        if self._connected:
            return

        self._closing = False
        self._session = aiohttp.ClientSession()

        try:
            # Specify protocol format explicitly to avoid deprecation warning
            # SurrealDB 2.0+ requires explicit format specification (json or cbor)
            self._ws = await self._session.ws_connect(
                self.url,
                timeout=ClientWSTimeout(ws_close=self.timeout),
                protocols=["json"],  # Explicit JSON format for RPC
            )
            self._connected = True

            # Start message reader loop
            self._reader_task = asyncio.create_task(self._read_loop())

            # Yield to event loop to allow read loop to start
            await asyncio.sleep(0)

            # Set namespace and database
            await self.use(self.namespace, self.database)

        except aiohttp.ClientError as e:
            await self._cleanup()
            raise ConnectionError(f"WebSocket connection failed: {e}")

    async def close(self) -> None:
        """Close WebSocket connection."""
        self._closing = True
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Clean up connection resources."""
        self._connected = False
        self._authenticated = False

        # Cancel reader task
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        # Cancel reconnect task
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None

        # Fail all pending requests
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Connection closed"))
        self._pending.clear()

        # Close WebSocket
        if self._ws:
            await self._ws.close()
            self._ws = None

        # Close session
        if self._session:
            await self._session.close()
            self._session = None

    async def _read_loop(self) -> None:
        """Background task to read WebSocket messages."""
        if not self._ws:
            return

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    # CBOR support could be added here
                    pass
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    break

        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if not self._closing and self.auto_reconnect:
                self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _handle_message(self, data: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            return

        msg_id = message.get("id")

        # Check if this is a response to a pending request
        # Convert msg_id to int for comparison since SurrealDB may return string IDs
        if msg_id is not None:
            try:
                msg_id_int = int(msg_id)
            except (ValueError, TypeError):
                msg_id_int = None

            if msg_id_int is not None and msg_id_int in self._pending:
                response = RPCResponse.from_dict(message)
                future = self._pending.pop(msg_id_int)
                if not future.done():
                    future.set_result(response)
                return

        # Check if this is a live query notification
        if "action" in message:
            live_id = message.get("id")
            if live_id and live_id in self._live_callbacks:
                callback = self._live_callbacks[live_id]
                asyncio.create_task(callback(message))

    async def _reconnect(self) -> None:
        """Attempt to reconnect after disconnection."""
        attempts = 0
        while attempts < self.max_reconnect_attempts and not self._closing:
            attempts += 1
            await asyncio.sleep(self.reconnect_interval)

            try:
                self._session = aiohttp.ClientSession()
                self._ws = await self._session.ws_connect(
                    self.url,
                    timeout=ClientWSTimeout(ws_close=self.timeout),
                    protocols=["json"],
                )
                self._connected = True
                self._reader_task = asyncio.create_task(self._read_loop())

                # Re-authenticate if we had a token
                if self._token:
                    await self.rpc("authenticate", [self._token])

                # Set namespace and database
                await self.use(self.namespace, self.database)

                # Re-establish live queries
                # Note: Live query UUIDs change on reconnect
                # Clients should handle this via callbacks

                return

            except Exception:
                await self._cleanup()
                continue

    async def _send_rpc(self, request: RPCRequest) -> RPCResponse:
        """
        Send RPC request via WebSocket.

        Args:
            request: The RPC request to send

        Returns:
            The RPC response

        Raises:
            ConnectionError: If not connected
            TimeoutError: If request times out
        """
        if not self._ws or not self._connected:
            raise ConnectionError("Not connected. Call connect() first.")

        request.id = self._next_request_id()

        # Create future for response
        loop = asyncio.get_running_loop()
        future: asyncio.Future[RPCResponse] = loop.create_future()
        self._pending[request.id] = future

        try:
            # Send request
            await self._ws.send_str(request.to_json())

            # Wait for response with timeout
            response = await asyncio.wait_for(future, timeout=self.timeout)
            return response

        except asyncio.TimeoutError:
            self._pending.pop(request.id, None)
            raise TimeoutError(f"Request timed out after {self.timeout}s")
        except Exception as e:
            self._pending.pop(request.id, None)
            raise ConnectionError(f"Request failed: {e}")

    # WebSocket-specific methods

    async def live(
        self,
        table: str,
        callback: LiveCallback,
        diff: bool = False,
    ) -> str:
        """
        Start a live query subscription.

        Args:
            table: Table to watch
            callback: Async callback for change notifications
            diff: If True, receive only changed fields

        Returns:
            Live query UUID

        Raises:
            LiveQueryError: If live query fails to start
        """
        sql = f"LIVE SELECT * FROM {table}"
        if diff:
            sql += " DIFF"

        try:
            response = await self.query(sql)

            # Extract live query UUID from result
            if response.results:
                first_result = response.results[0]
                if first_result.is_ok:
                    # Result can be string UUID directly or dict with "result" key
                    if isinstance(first_result.result, str):
                        live_id = first_result.result
                    elif isinstance(first_result.result, dict) and "result" in first_result.result:
                        live_id = str(first_result.result["result"])
                    else:
                        raise LiveQueryError("Invalid live query response")

                    self._live_callbacks[live_id] = callback
                    return live_id

            raise LiveQueryError("No live query ID returned")

        except Exception as e:
            raise LiveQueryError(f"Failed to start live query: {e}")

    async def kill(self, live_id: str) -> None:
        """
        Stop a live query subscription.

        Args:
            live_id: Live query UUID to stop
        """
        await self.rpc("kill", [live_id])
        self._live_callbacks.pop(live_id, None)

    async def let(self, name: str, value: Any) -> None:
        """
        Set a session variable.

        Args:
            name: Variable name
            value: Variable value
        """
        await self.rpc("let", [name, value])

    async def unset(self, name: str) -> None:
        """
        Remove a session variable.

        Args:
            name: Variable name to remove
        """
        await self.rpc("unset", [name])

    @property
    def live_queries(self) -> list[str]:
        """Get list of active live query IDs."""
        return list(self._live_callbacks.keys())

    async def kill_all_live_queries(self) -> None:
        """Stop all active live queries."""
        for live_id in list(self._live_callbacks.keys()):
            try:
                await self.kill(live_id)
            except Exception:
                pass

    # Transaction support

    def transaction(self) -> "WebSocketTransaction":
        """
        Create a new WebSocket transaction.

        WebSocket transactions use server-side state with BEGIN/COMMIT/ROLLBACK.
        Operations are executed immediately within the transaction context.

        Usage:
            async with conn.transaction() as tx:
                await tx.create("users", {"name": "Alice"})
                await tx.create("orders", {"user": "users:alice"})
                # Committed on successful exit, rolled back on exception

        Returns:
            WebSocketTransaction context manager
        """
        from ..transaction import WebSocketTransaction

        return WebSocketTransaction(self)
