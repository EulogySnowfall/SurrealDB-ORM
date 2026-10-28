"""Tests for HTTP connection module."""

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import httpx
import pytest

from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.exceptions import AuthenticationError, ConnectionError, QueryError


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


class _FakeHTTPClient:
    """Stand-in for ``httpx.AsyncClient`` routing ``/signin`` and ``/rpc`` posts.

    ``/signin`` mints ``root-token-<n>`` (or answers *signin_status*). ``/rpc``
    answers 401 for any bearer token in *rejected_tokens* and otherwise a
    JSON-RPC result chosen by method: ``signup`` returns ``signup-token``.
    Every call is recorded as ``(path, method_or_None, authorization)``.
    """

    def __init__(self, rejected_tokens: set[str] | None = None, signin_status: int = 200) -> None:
        self.rejected_tokens = rejected_tokens if rejected_tokens is not None else set()
        self.signin_status = signin_status
        self.calls: list[tuple[str, str | None, str | None]] = []
        self._minted = 0

    def signin_count(self) -> int:
        return sum(1 for path, _, _ in self.calls if path == "/signin")

    def rpc_methods(self) -> list[str | None]:
        return [method for path, method, _ in self.calls if path == "/rpc"]

    async def post(
        self, path: str, json: object = None, content: bytes | str | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        import json as json_module

        request = httpx.Request("POST", f"http://localhost:8000{path}")
        authorization = (headers or {}).get("Authorization")
        if path == "/signin":
            self.calls.append((path, None, authorization))
            if self.signin_status != 200:
                return httpx.Response(self.signin_status, request=request, text="No signin target")
            self._minted += 1
            return httpx.Response(200, request=request, json={"token": f"root-token-{self._minted}"})
        body = json_module.loads(content or b"{}")
        method = body.get("method")
        self.calls.append((path, method, authorization))
        token = authorization.removeprefix("Bearer ") if authorization else None
        if token in self.rejected_tokens:
            return httpx.Response(401, request=request, text="There was a problem with authentication")
        result: object = "signup-token" if method == "signup" else [{"result": [], "status": "OK"}]
        return httpx.Response(200, request=request, json={"id": body.get("id"), "result": result})

    async def aclose(self) -> None:
        return None


class TestReauthOnlyForSigninIdentity:
    """Issue #349: the 401 re-mint only replays the credentials the current token came from.

    ``authenticate()`` and ``signup()`` switch the connection to another identity; a
    later 401 on that identity's token must surface instead of signing in again with
    the arguments of an earlier ``signin()``.
    """

    def _conn(self, monkeypatch: pytest.MonkeyPatch, fake: _FakeHTTPClient) -> HTTPConnection:
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="json")
        conn._connected = True
        conn._client = fake  # type: ignore[assignment]
        monkeypatch.setattr("src.surreal_sdk.connection.http.asyncio.sleep", AsyncMock())
        return conn

    async def test_401_after_authenticate_raises_without_new_signin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        await conn.authenticate("record-token")
        fake.rejected_tokens.add("record-token")  # the record token expires

        with pytest.raises(QueryError, match="401"):
            await conn.query("RETURN 1")

        assert fake.signin_count() == 1  # only the explicit signin, never replayed
        assert conn.token == "record-token"
        assert conn._signin_args is None

    async def test_401_after_signup_raises_without_new_signin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        await conn.signup("ns", "db", "account", email="a@b.c", password="pw")
        assert conn.token == "signup-token"
        fake.rejected_tokens.add("signup-token")

        with pytest.raises(QueryError, match="401"):
            await conn.query("RETURN 1")

        assert fake.signin_count() == 1
        assert conn._signin_args is None

    async def test_failed_signin_clears_retained_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        fake.signin_status = 401

        with pytest.raises(AuthenticationError):
            await conn.signin("someone", "wrong")

        assert conn._signin_args is None

    async def test_failed_authenticate_clears_retained_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        # The authenticate RPC is rejected, and so is the token it travels with.
        fake.rejected_tokens.add("root-token-1")
        fake.signin_status = 401

        with pytest.raises(AuthenticationError):
            await conn.authenticate("bad-token")

        assert conn._signin_args is None
        assert conn.token is None
        assert not conn.is_authenticated

    async def test_transient_401_after_signin_still_remints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        fake.rejected_tokens.add("root-token-1")  # nbf momentarily in the future

        response = await conn.query("RETURN 1")

        assert response.is_ok
        assert fake.signin_count() == 2  # re-minted once
        assert conn.token == "root-token-2"
        assert conn._signin_args is not None

    async def test_failed_remint_makes_the_retry_the_last_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        fake.rejected_tokens.add("root-token-1")
        fake.signin_status = 401  # the re-mint itself is refused

        with pytest.raises(QueryError, match="401"):
            await conn.query("RETURN 1")

        assert fake.signin_count() == 2  # explicit signin + one failed re-mint
        assert fake.rpc_methods() == ["query", "query"]  # first attempt + one retry
        assert conn._signin_args is None

    async def test_close_clears_retained_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")

        await conn.close()

        assert conn._signin_args is None

    async def test_invalidate_sends_rpc_and_resets_auth_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")

        await conn.invalidate()

        assert fake.calls[-1] == ("/rpc", "invalidate", "Bearer root-token-1")
        assert conn.token is None
        assert not conn.is_authenticated
        assert conn._signin_args is None

        await conn.query("RETURN 1")
        assert fake.calls[-1][2] is None  # next request carries no credentials

    async def test_invalidate_resets_state_even_when_rpc_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        fake.rejected_tokens.add("root-token-1")  # token already expired

        with pytest.raises(QueryError, match="401"):
            await conn.invalidate()

        assert fake.signin_count() == 1  # invalidate never signs in again
        assert conn.token is None
        assert not conn.is_authenticated
        assert conn._signin_args is None
