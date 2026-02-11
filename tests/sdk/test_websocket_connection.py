"""Tests for WebSocket connection module."""

from typing import Any, AsyncGenerator
import pytest

from src.surreal_sdk.connection.websocket import WebSocketConnection


class TestWebSocketConnection:
    """Tests for WebSocketConnection class."""

    def test_init_normalizes_url(self) -> None:
        """Test URL normalization."""
        # WS URL with /rpc suffix
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        assert conn.url == "ws://localhost:8000/rpc"

        # HTTP URL gets converted
        conn = WebSocketConnection("http://localhost:8000", "ns", "db")
        assert conn.url == "ws://localhost:8000/rpc"

        # HTTPS URL gets converted
        conn = WebSocketConnection("https://localhost:8000", "ns", "db")
        assert conn.url == "wss://localhost:8000/rpc"

        # Already has /rpc suffix
        conn = WebSocketConnection("ws://localhost:8000/rpc", "ns", "db")
        assert conn.url == "ws://localhost:8000/rpc"

    def test_default_settings(self) -> None:
        """Test default connection settings."""
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")

        assert conn.auto_reconnect is True
        assert conn.reconnect_interval == 1.0
        assert conn.max_reconnect_attempts == 5
        assert conn.timeout == 30.0

    def test_custom_settings(self) -> None:
        """Test custom connection settings."""
        conn = WebSocketConnection(
            "ws://localhost:8000",
            "ns",
            "db",
            auto_reconnect=False,
            reconnect_interval=2.0,
            max_reconnect_attempts=10,
            timeout=60.0,
        )

        assert conn.auto_reconnect is False
        assert conn.reconnect_interval == 2.0
        assert conn.max_reconnect_attempts == 10
        assert conn.timeout == 60.0

    def test_request_id_increments(self) -> None:
        """Test that request IDs increment."""
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")

        assert conn._next_request_id() == 1
        assert conn._next_request_id() == 2
        assert conn._next_request_id() == 3

    def test_live_queries_property(self) -> None:
        """Test live_queries property."""
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")

        assert conn.live_queries == []

        # Simulate adding a live query
        async def dummy_callback(data: Any) -> None:
            pass

        conn._live_callbacks["uuid-1"] = dummy_callback
        conn._live_callbacks["uuid-2"] = dummy_callback

        assert len(conn.live_queries) == 2
        assert "uuid-1" in conn.live_queries
        assert "uuid-2" in conn.live_queries


class TestWebSocketConnectionIntegration:
    """Integration tests requiring a running SurrealDB instance."""

    @pytest.fixture(scope="function")
    async def connection(self) -> AsyncGenerator[WebSocketConnection, None]:
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
            yield conn
        finally:
            await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_signin(self, connection: WebSocketConnection) -> None:
        """Test authentication."""
        assert connection.is_authenticated

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query(self, connection: WebSocketConnection) -> None:
        """Test query execution."""
        result = await connection.query("INFO FOR DB")
        assert result is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_session_variables(self, connection: WebSocketConnection) -> None:
        """Test session variable operations."""
        # Set variable
        await connection.let("my_var", 42)

        # Use variable in query
        _ = await connection.query("RETURN $my_var")
        # Note: Result format may vary

        # Unset variable
        await connection.unset("my_var")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ping(self, connection: WebSocketConnection) -> None:
        """Test ping."""
        result = await connection.ping()
        assert result is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_live_query(self, connection: WebSocketConnection) -> None:
        """Test live query subscription."""
        notifications: list[Any] = []

        async def callback(data: Any) -> None:
            notifications.append(data)

        # Subscribe
        live_id = await connection.live("test_live", callback)
        assert live_id is not None
        assert live_id in connection.live_queries

        # Kill
        await connection.kill(live_id)
        assert live_id not in connection.live_queries
