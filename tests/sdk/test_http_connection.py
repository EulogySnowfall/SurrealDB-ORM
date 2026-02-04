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

    def test_headers_without_token_cbor(self) -> None:
        """Test headers when not authenticated (default CBOR protocol)."""
        conn = HTTPConnection("http://localhost:8000", "test_ns", "test_db")
        headers = conn.headers

        assert headers["Surreal-NS"] == "test_ns"
        assert headers["Surreal-DB"] == "test_db"
        # Default protocol is CBOR
        assert headers["Accept"] == "application/cbor"
        assert headers["Content-Type"] == "application/cbor"
        assert "Authorization" not in headers

    def test_headers_without_token_json(self) -> None:
        """Test headers when not authenticated (JSON protocol)."""
        conn = HTTPConnection("http://localhost:8000", "test_ns", "test_db", protocol="json")
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


class TestHTTPConnectionProtocol:
    """Tests for protocol configuration."""

    def test_default_protocol_is_cbor(self) -> None:
        """HTTPConnection should default to CBOR protocol."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db")
        assert conn.protocol == "cbor"

    def test_cbor_protocol_explicit(self) -> None:
        """HTTPConnection should accept explicit CBOR protocol."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="cbor")
        assert conn.protocol == "cbor"

    def test_json_protocol(self) -> None:
        """HTTPConnection should accept JSON protocol."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="json")
        assert conn.protocol == "json"

    def test_invalid_protocol_raises(self) -> None:
        """HTTPConnection should reject invalid protocols."""
        with pytest.raises(ValueError, match="Invalid protocol"):
            HTTPConnection("http://localhost:8000", "ns", "db", protocol="xml")  # type: ignore

    def test_cbor_headers(self) -> None:
        """CBOR protocol should set correct headers."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="cbor")
        headers = conn.headers
        assert headers["Accept"] == "application/cbor"
        assert headers["Content-Type"] == "application/cbor"

    def test_json_headers(self) -> None:
        """JSON protocol should set correct headers."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="json")
        headers = conn.headers
        assert headers["Accept"] == "application/json"
        assert headers["Content-Type"] == "application/json"

    def test_timeout_parameter(self) -> None:
        """HTTPConnection should accept timeout parameter."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db", timeout=60.0)
        assert conn.timeout == 60.0

    def test_default_timeout(self) -> None:
        """HTTPConnection should have default timeout."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db")
        assert conn.timeout == 30.0


class TestHTTPConnectionIntegration:
    """Integration tests requiring a running SurrealDB instance."""

    @pytest.fixture(scope="function")
    async def connection(self) -> AsyncGenerator[HTTPConnection, None]:
        """Create a connected HTTP connection."""
        conn = HTTPConnection("http://localhost:8001", "test", "test")
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
