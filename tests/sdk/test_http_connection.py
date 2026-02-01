"""Tests for HTTP connection module."""

from typing import AsyncGenerator
import pytest

from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.exceptions import ConnectionError


class TestHTTPConnection:
    """Tests for HTTPConnection class."""

    def test_init_normalizes_url(self) -> None:
        """Test URL normalization."""
        # HTTP URL stays as is
        conn = HTTPConnection("http://localhost:8000", "ns", "db")
        assert conn.url == "http://localhost:8000"

        # WS URL gets converted
        conn = HTTPConnection("ws://localhost:8000", "ns", "db")
        assert conn.url == "http://localhost:8000"

        # WSS URL gets converted
        conn = HTTPConnection("wss://localhost:8000", "ns", "db")
        assert conn.url == "https://localhost:8000"

        # Trailing slash removed
        conn = HTTPConnection("http://localhost:8000/", "ns", "db")
        assert conn.url == "http://localhost:8000"

    def test_headers_without_token(self) -> None:
        """Test headers when not authenticated."""
        conn = HTTPConnection("http://localhost:8000", "test_ns", "test_db")
        headers = conn.headers

        assert headers["Surreal-NS"] == "test_ns"
        assert headers["Surreal-DB"] == "test_db"
        assert headers["Accept"] == "application/json"
        assert headers["Content-Type"] == "application/json"
        assert "Authorization" not in headers

    def test_headers_with_token(self) -> None:
        """Test headers when authenticated."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db")
        conn._token = "test-jwt-token"
        headers = conn.headers

        assert headers["Authorization"] == "Bearer test-jwt-token"

    @pytest.mark.asyncio
    async def test_connect(self) -> None:
        """Test connection establishment."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db")

        assert not conn.is_connected
        await conn.connect()
        assert conn.is_connected
        assert conn._client is not None

        await conn.close()
        assert not conn.is_connected

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        """Test async context manager."""
        async with HTTPConnection("http://localhost:8000", "ns", "db") as conn:
            assert conn.is_connected

        assert not conn.is_connected

    @pytest.mark.asyncio
    async def test_send_rpc_not_connected(self) -> None:
        """Test RPC call when not connected."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db")

        with pytest.raises(ConnectionError, match="Not connected"):
            from src.surreal_sdk.protocol.rpc import RPCRequest

            await conn._send_rpc(RPCRequest(method="ping"))

    @pytest.mark.asyncio
    async def test_health_check(self) -> None:
        """Test health check endpoint."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db")

        # Not connected should return False
        result = await conn.health()
        assert result is False

    @pytest.mark.asyncio
    async def test_request_id_increments(self) -> None:
        """Test that request IDs increment."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db")

        assert conn._next_request_id() == 1
        assert conn._next_request_id() == 2
        assert conn._next_request_id() == 3


class TestHTTPConnectionIntegration:
    """Integration tests requiring a running SurrealDB instance."""

    @pytest.fixture(scope="function")
    async def connection(self) -> AsyncGenerator[HTTPConnection, None]:
        """Create a connected HTTP connection."""
        conn = HTTPConnection("http://localhost:8000", "test", "test")
        try:
            await conn.connect()
            await conn.signin("root", "root")
            yield conn
        finally:
            await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_signin(self, connection: HTTPConnection) -> None:
        """Test authentication."""
        # Connection is already authenticated in fixture
        assert connection.is_authenticated
        assert connection.token is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_query(self, connection: HTTPConnection) -> None:
        """Test query execution."""
        result = await connection.query("INFO FOR DB")
        assert result is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_crud_operations(self, connection: HTTPConnection) -> None:
        """Test CRUD operations."""
        # Create
        record = await connection.create("test_users", {"name": "Alice", "age": 30})
        assert record is not None

        # Select
        records = await connection.select("test_users")
        assert records.count > 0

        # Delete table
        await connection.query("DELETE test_users")
