"""Tests for HTTP connection module."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from unittest.mock import AsyncMock

import httpx
import pytest

from src.surreal_sdk.connection import http as http_module
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.exceptions import ConnectionError, QueryError
from src.surreal_sdk.protocol.rpc import RPCRequest


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
            # SurrealDB 3.0 requires namespace/database to exist before use
            await conn.query("DEFINE NAMESPACE IF NOT EXISTS test; DEFINE DATABASE IF NOT EXISTS test;")
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


def _resp(status: int, json_body: dict[str, object] | None = None) -> httpx.Response:
    """Build a real httpx.Response so ``raise_for_status()`` behaves correctly."""
    request = httpx.Request("POST", "http://localhost:8000/rpc")
    if json_body is not None:
        return httpx.Response(status, request=request, json=json_body)
    return httpx.Response(status, request=request, text="There was a problem with authentication")


class TestTransientAuthRetry:
    """Regression tests for issue #101 — transient 401s from JWT ``nbf`` clock skew.

    A freshly-minted token can be rejected with a 401 when the server clock jumps
    backwards (notably on WSL2). ``_send_rpc`` must transparently re-mint the
    token and retry, rather than surfacing the transient failure.
    """

    def _signed_in_conn(self, monkeypatch: pytest.MonkeyPatch) -> HTTPConnection:
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="json")
        conn._connected = True
        conn._token = "stale.jwt.token"
        conn._signin_args = {"user": "root", "password": "root", "namespace": None, "database": None, "access": None}
        # Avoid real backoff delays in the retry loop.
        monkeypatch.setattr("src.surreal_sdk.connection.http.asyncio.sleep", AsyncMock())
        return conn

    @pytest.mark.asyncio
    async def test_transient_401_is_retried_after_remint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 401 followed by success should re-mint the token and return the result."""
        conn = self._signed_in_conn(monkeypatch)
        conn._client = AsyncMock()
        conn._client.post = AsyncMock(side_effect=[_resp(401), _resp(200, {"id": 1, "result": [{"ok": True}]})])
        signin_mock = AsyncMock()
        monkeypatch.setattr(conn, "signin", signin_mock)

        response = await conn._send_rpc(RPCRequest(method="query", params=["INFO FOR DB", {}]))

        assert response.is_success
        assert response.result == [{"ok": True}]
        assert conn._client.post.await_count == 2  # original + one retry
        signin_mock.assert_awaited_once()  # token was re-minted

    @pytest.mark.asyncio
    async def test_persistent_401_eventually_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If re-minting never helps, the retry budget is bounded and a 401 is raised."""
        conn = self._signed_in_conn(monkeypatch)
        conn._client = AsyncMock()
        conn._client.post = AsyncMock(return_value=_resp(401))
        monkeypatch.setattr(conn, "signin", AsyncMock())

        with pytest.raises(QueryError, match="401"):
            await conn._send_rpc(RPCRequest(method="query", params=["INFO FOR DB", {}]))

        # Bounded: first attempt + one retry per configured backoff.
        assert conn._client.post.await_count == len(http_module._AUTH_RETRY_BACKOFFS_S) + 1

    @pytest.mark.asyncio
    async def test_401_without_credentials_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 401 with no retained signin credentials is a genuine failure — no retry."""
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="json")
        conn._connected = True
        conn._client = AsyncMock()
        conn._client.post = AsyncMock(return_value=_resp(401))
        monkeypatch.setattr("src.surreal_sdk.connection.http.asyncio.sleep", AsyncMock())

        with pytest.raises(QueryError, match="401"):
            await conn._send_rpc(RPCRequest(method="query", params=["INFO FOR DB", {}]))

        assert conn._client.post.await_count == 1  # no retry attempted


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
        self, path: str, json: object = None, content: bytes | None = None, headers: dict[str, str] | None = None
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
    """Issue #330: the 401 re-mint must only replay credentials the current token came from.

    ``authenticate()`` and ``signup()`` switch the connection to another identity; a
    later 401 on that identity's token must surface instead of signing in again with
    the arguments of an earlier ``signin()``.
    """

    async def _conn(self, monkeypatch: pytest.MonkeyPatch, fake: _FakeHTTPClient) -> HTTPConnection:
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="json")
        conn._connected = True
        conn._client = fake  # type: ignore[assignment]
        monkeypatch.setattr("src.surreal_sdk.connection.http.asyncio.sleep", AsyncMock())
        return conn

    @pytest.mark.asyncio
    async def test_401_after_authenticate_raises_without_new_signin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        await conn.authenticate("record-token")
        fake.rejected_tokens.add("record-token")  # the record token expires

        with pytest.raises(QueryError, match="401"):
            await conn.query("INFO FOR ROOT")

        assert fake.signin_count() == 1  # only the explicit signin, never replayed
        assert conn.token == "record-token"
        assert conn._signin_args is None

    @pytest.mark.asyncio
    async def test_401_after_signup_raises_without_new_signin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        await conn.signup("ns", "db", "account", email="a@b.c", password="pw")
        assert conn.token == "signup-token"
        fake.rejected_tokens.add("signup-token")

        with pytest.raises(QueryError, match="401"):
            await conn.query("INFO FOR ROOT")

        assert fake.signin_count() == 1
        assert conn._signin_args is None

    @pytest.mark.asyncio
    async def test_failed_signin_clears_retained_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.surreal_sdk.exceptions import AuthenticationError

        fake = _FakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        fake.signin_status = 401

        with pytest.raises(AuthenticationError):
            await conn.signin("someone", "wrong")

        assert conn._signin_args is None

    @pytest.mark.asyncio
    async def test_failed_authenticate_clears_retained_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.surreal_sdk.exceptions import AuthenticationError

        fake = _FakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        # The authenticate RPC is rejected, and so is the root token it travels with.
        fake.rejected_tokens.add("root-token-1")
        fake.signin_status = 401

        with pytest.raises(AuthenticationError):
            await conn.authenticate("bad-token")

        assert conn._signin_args is None
        assert conn.token is None
        assert not conn.is_authenticated

    @pytest.mark.asyncio
    async def test_transient_401_after_signin_still_remints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        fake.rejected_tokens.add("root-token-1")  # nbf momentarily in the future

        response = await conn.query("INFO FOR DB")

        assert response.is_ok
        assert fake.signin_count() == 2  # re-minted once
        assert conn.token == "root-token-2"
        assert conn._signin_args is not None

    @pytest.mark.asyncio
    async def test_close_clears_retained_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        await conn.signin("root", "root")

        await conn.close()

        assert conn._signin_args is None

    @pytest.mark.asyncio
    async def test_invalidate_sends_rpc_and_resets_auth_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        await conn.signin("root", "root")

        await conn.invalidate()

        assert fake.calls[-1] == ("/rpc", "invalidate", "Bearer root-token-1")
        assert conn.token is None
        assert not conn.is_authenticated
        assert conn._signin_args is None

        await conn.query("RETURN 1")
        assert fake.calls[-1][2] is None  # next request carries no credentials

    @pytest.mark.asyncio
    async def test_invalidate_resets_state_even_when_rpc_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        await conn.signin("root", "root")
        fake.rejected_tokens.add("root-token-1")  # token already expired

        with pytest.raises(QueryError, match="401"):
            await conn.invalidate()

        assert fake.signin_count() == 1  # invalidate never re-signs in
        assert conn.token is None
        assert not conn.is_authenticated
        assert conn._signin_args is None


class _GatedFakeHTTPClient(_FakeHTTPClient):
    """``_FakeHTTPClient`` that can hold one ``/signin`` or one rejected ``/rpc`` reply.

    ``hold_next_signin()`` / ``hold_next_rejection()`` make the next matching post
    set ``held`` and wait for ``release`` before answering, so a test can run
    another task's auth call at that exact point. The rejection is decided
    before the hold, from the token the request was sent with.
    """

    def __init__(self) -> None:
        super().__init__()
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self._hold: str | None = None

    def hold_next_signin(self) -> None:
        self._hold = "signin"

    def hold_next_rejection(self) -> None:
        self._hold = "rejection"

    async def _wait_if_held(self, kind: str) -> None:
        if self._hold == kind:
            self._hold = None
            self.held.set()
            await self.release.wait()

    async def post(
        self, path: str, json: object = None, content: bytes | None = None, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        if path == "/signin":
            await self._wait_if_held("signin")
            return await super().post(path, json=json, content=content, headers=headers)
        response = await super().post(path, json=json, content=content, headers=headers)
        if response.status_code == 401:
            await self._wait_if_held("rejection")
        return response


class TestReauthDoesNotOverrideConcurrentAuthChange:
    """Issue #390: a pending re-mint must not undo an auth change made by another task.

    Task A gets a 401 and re-signs in with the retained ``signin()`` arguments.
    If task B changes the connection's auth state meanwhile, A's re-minted token
    is discarded. A's request is replayed only under the same identity, i.e. a
    token renewed from the same ``signin()``; otherwise A raises the 401 and
    nothing of A's runs with B's credentials.
    """

    async def _conn(self, monkeypatch: pytest.MonkeyPatch, fake: _GatedFakeHTTPClient) -> HTTPConnection:
        conn = HTTPConnection("http://localhost:8000", "ns", "db", protocol="json")
        conn._connected = True
        conn._client = fake  # type: ignore[assignment]
        monkeypatch.setattr("src.surreal_sdk.connection.http.asyncio.sleep", AsyncMock())
        await conn.signin("root", "root")  # root-token-1
        fake.rejected_tokens.add("root-token-1")  # transient 401 for task A
        return conn

    async def _run_during_remint(
        self, conn: HTTPConnection, fake: _GatedFakeHTTPClient, other: Callable[[], Awaitable[object]]
    ) -> BaseException | None:
        """Start A's query, run *other* while A's re-signin is pending, then let it complete.

        Returns the exception A raised, or ``None`` if A's query succeeded.
        """
        fake.hold_next_signin()
        task_a = asyncio.create_task(conn.query("SELECT * FROM a_only"))
        await asyncio.wait_for(fake.held.wait(), timeout=5)
        fake.rejected_tokens.discard("root-token-1")  # the skew window has passed for B
        await other()
        fake.release.set()
        try:
            await asyncio.wait_for(task_a, timeout=5)
        except Exception as exc:
            return exc
        return None

    @staticmethod
    def _queries_sent_with(fake: _GatedFakeHTTPClient, authorization: str | None) -> int:
        return sum(1 for path, method, auth in fake.calls if path == "/rpc" and method == "query" and auth == authorization)

    @pytest.mark.asyncio
    async def test_authenticate_during_remint_keeps_the_new_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _GatedFakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)

        error = await self._run_during_remint(conn, fake, lambda: conn.authenticate("user-token"))

        assert isinstance(error, QueryError) and error.code == 401
        assert self._queries_sent_with(fake, "Bearer user-token") == 0  # A never replayed as B
        assert conn.token == "user-token"
        assert conn.is_authenticated
        assert conn._signin_args is None
        await conn.query("RETURN 1")
        assert fake.calls[-1][2] == "Bearer user-token"

    @pytest.mark.asyncio
    async def test_invalidate_during_remint_stays_unauthenticated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _GatedFakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)

        error = await self._run_during_remint(conn, fake, conn.invalidate)

        assert isinstance(error, QueryError) and error.code == 401
        assert self._queries_sent_with(fake, None) == 0  # A not replayed without credentials
        assert conn.token is None
        assert not conn.is_authenticated
        assert conn._signin_args is None
        await conn.query("RETURN 1")
        assert fake.calls[-1][2] is None

    @pytest.mark.asyncio
    async def test_close_during_remint_stays_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _GatedFakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)

        error = await self._run_during_remint(conn, fake, conn.close)

        assert isinstance(error, QueryError) and error.code == 401
        assert conn._client is None
        assert not conn.is_connected
        assert not conn.is_authenticated
        assert conn._signin_args is None
        assert conn.token != "root-token-2"  # the re-minted token was thrown away

    @pytest.mark.asyncio
    async def test_signup_during_remint_keeps_the_signup_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _GatedFakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)

        error = await self._run_during_remint(
            conn, fake, lambda: conn.signup("ns", "db", "account", email="a@b.c", password="pw")
        )

        assert isinstance(error, QueryError) and error.code == 401
        assert self._queries_sent_with(fake, "Bearer signup-token") == 0
        assert conn.token == "signup-token"
        assert conn.is_authenticated
        assert conn._signin_args is None

    @pytest.mark.asyncio
    async def test_other_signin_during_remint_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _GatedFakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)

        error = await self._run_during_remint(conn, fake, lambda: conn.signin("other", "pw"))

        assert isinstance(error, QueryError) and error.code == 401
        assert conn.token == "root-token-2"  # B's signin, not A's discarded re-mint
        assert conn._signin_args is not None and conn._signin_args["user"] == "other"
        assert self._queries_sent_with(fake, "Bearer root-token-2") == 0

    @pytest.mark.asyncio
    async def test_identity_changed_before_the_401_raises_without_signin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _GatedFakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        fake.hold_next_rejection()

        task_a = asyncio.create_task(conn.query("SELECT * FROM a_only"))
        await asyncio.wait_for(fake.held.wait(), timeout=5)
        fake.rejected_tokens.discard("root-token-1")
        await conn.authenticate("user-token")
        fake.release.set()

        with pytest.raises(QueryError, match="401"):
            await asyncio.wait_for(task_a, timeout=5)
        assert fake.signin_count() == 1  # only the explicit signin: no re-mint
        assert self._queries_sent_with(fake, "Bearer user-token") == 0
        assert conn.token == "user-token"
        assert conn._signin_args is None

    @pytest.mark.asyncio
    async def test_token_renewed_by_another_task_is_reused_without_signin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _GatedFakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)
        fake.hold_next_rejection()

        task_a = asyncio.create_task(conn.query("SELECT * FROM a_only"))
        await asyncio.wait_for(fake.held.wait(), timeout=5)
        # Task B hits the same transient 401 and renews the token of the same signin().
        await conn.query("RETURN 1")
        assert conn.token == "root-token-2"
        fake.release.set()
        response = await asyncio.wait_for(task_a, timeout=5)

        assert response.is_ok
        assert fake.signin_count() == 2  # the first signin and B's re-mint; A did not sign in
        assert fake.calls[-1] == ("/rpc", "query", "Bearer root-token-2")  # A's retry
        assert conn._signin_args is not None

    @pytest.mark.asyncio
    async def test_lone_401_still_remints_and_installs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _GatedFakeHTTPClient()
        conn = await self._conn(monkeypatch, fake)

        response = await conn.query("INFO FOR DB")

        assert response.is_ok
        assert fake.signin_count() == 2
        assert conn.token == "root-token-2"
        assert conn._signin_args is not None
        assert fake.calls[-1] == ("/rpc", "query", "Bearer root-token-2")
