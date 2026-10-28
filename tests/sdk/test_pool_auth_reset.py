"""Issue #391 — a pooled connection is returned to the pool's own identity on release.

A caller holding a connection from ``ConnectionPool.acquire()`` may change its
authentication state (``authenticate()``, ``signup()``, ``signin()``,
``invalidate()``). The next caller to acquire the same connection must get the
pool's identity — signed in with the pool's credentials, or unauthenticated when
the pool has none — and a connection whose reset fails must be closed, never
handed out again.

The unit tests drive real ``HTTPConnection`` / ``WebSocketConnection`` objects
whose transport is replaced by an in-memory fake; the integration tests check
``$session`` on the pinned server.
"""

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest

from src.surreal_sdk.connection.base import BaseSurrealConnection
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.pool import ConnectionPool
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.exceptions import QueryError
from src.surreal_sdk.protocol.rpc import RPCError, RPCRequest, RPCResponse

URL = "http://localhost:8000"
NS = "ns"
DB = "db"
POOL_USER = "i391_pool_user"
POOL_PASS = "i391_pool_pass"
CALLER_TOKEN = "i391-caller-token"


class _FakeServer:
    """Per-connection server state: who the session is, and which RPCs ran."""

    def __init__(self) -> None:
        self.identity: str | None = None
        self.methods: list[str] = []
        self.fail_signin = False
        self.signins = 0

    def signin_token(self, user: str | None) -> str:
        self.signins += 1
        if self.fail_signin:
            raise RuntimeError("signin rejected")
        self.identity = f"user:{user}"
        return f"token-for-{user}-{self.signins}"

    def handle(self, request: RPCRequest) -> RPCResponse:
        method = str(request.method)
        self.methods.append(method)
        params = request.params
        result: Any = None
        if method == "signin":
            assert isinstance(params, dict)
            try:
                result = self.signin_token(params.get("user"))
            except RuntimeError as e:
                return RPCResponse(id=request.id or 0, error=RPCError(code=-32000, message=str(e)))
        elif method == "authenticate":
            assert isinstance(params, list)
            self.identity = f"token:{params[0]}"
        elif method == "signup":
            self.identity = "signup:record"
            result = "i391-signup-token"
        elif method == "invalidate":
            self.identity = None
        elif method == "query":
            result = [{"status": "OK", "result": self.identity}]
        return RPCResponse(id=request.id or 0, result=result)


def _wire(conn: BaseSurrealConnection, servers: list[_FakeServer]) -> None:
    """Replace *conn*'s transport with a fresh :class:`_FakeServer`."""
    server = _FakeServer()
    servers.append(server)

    async def send_rpc(request: RPCRequest) -> RPCResponse:
        if isinstance(conn, HTTPConnection) and conn._token is not None:
            # HTTP is stateless: the session is whatever the bearer token says.
            server.identity = conn._token
        return server.handle(request)

    conn._send_rpc = send_rpc  # type: ignore[method-assign]

    if isinstance(conn, HTTPConnection):

        def http_handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/signin"
            body = json.loads(request.content)
            try:
                token = server.signin_token(body.get("user"))
            except RuntimeError as e:
                return httpx.Response(401, text=str(e))
            return httpx.Response(200, json={"token": token})

        async def http_connect() -> HTTPConnection:
            conn._client = httpx.AsyncClient(base_url=conn.url, transport=httpx.MockTransport(http_handler))
            conn._connected = True
            return conn

        conn.connect = http_connect  # type: ignore[method-assign]
    else:

        async def ws_connect() -> WebSocketConnection:
            conn._connected = True
            return conn  # type: ignore[return-value]

        async def ws_close() -> None:
            conn._connected = False
            conn._authenticated = False

        conn.connect = ws_connect  # type: ignore[method-assign]
        conn.close = ws_close  # type: ignore[method-assign]


def _pool(kind: str, servers: list[_FakeServer], size: int = 1) -> ConnectionPool:
    pool = ConnectionPool(URL, NS, DB, size=size, connection_type=kind)
    original = pool._create_connection

    def create() -> BaseSurrealConnection:
        conn = original()
        _wire(conn, servers)
        return conn

    pool._create_connection = create  # type: ignore[method-assign]
    return pool


KINDS = ["http", "websocket"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_authenticate_by_caller_does_not_reach_next_caller(kind: str) -> None:
    servers: list[_FakeServer] = []
    async with _pool(kind, servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            first = conn
            await conn.authenticate(CALLER_TOKEN)
            assert conn.token == CALLER_TOKEN

        async with pool.acquire() as conn:
            assert conn is first
            assert conn.token is not None and conn.token != CALLER_TOKEN
            assert conn.token.startswith(f"token-for-{POOL_USER}")
            assert conn.is_authenticated
            response = await conn.query("RETURN $auth")
            assert response.first_result is not None
            assert CALLER_TOKEN not in str(response.first_result.result)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_signup_by_caller_does_not_reach_next_caller(kind: str) -> None:
    servers: list[_FakeServer] = []
    async with _pool(kind, servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            first = conn
            await conn.signup(NS, DB, "i391_access", email="someone@example.com")
            assert conn.token == "i391-signup-token"

        async with pool.acquire() as conn:
            assert conn is first
            assert conn.token is not None and conn.token.startswith(f"token-for-{POOL_USER}")
            assert servers[0].identity is not None and "signup" not in servers[0].identity


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_websocket_reconnect_token_and_http_replay_follow_the_reset(kind: str) -> None:
    """The token a WebSocket replays on reconnect, and the credentials HTTP replays on a 401, are the pool's."""
    servers: list[_FakeServer] = []
    async with _pool(kind, servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            await conn.authenticate(CALLER_TOKEN)
        async with pool.acquire() as conn:
            assert conn._token is not None and conn._token.startswith(f"token-for-{POOL_USER}")
            if isinstance(conn, HTTPConnection):
                assert conn._signin_args is not None
                assert conn._signin_args["user"] == POOL_USER


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("change", ["authenticate", "signin"])
async def test_pool_without_credentials_returns_connection_unauthenticated(kind: str, change: str) -> None:
    servers: list[_FakeServer] = []
    async with _pool(kind, servers) as pool:
        async with pool.acquire() as conn:
            first = conn
            assert not conn.is_authenticated
            if change == "authenticate":
                await conn.authenticate(CALLER_TOKEN)
            else:
                await conn.signin("i391_caller", "i391_caller_pass")
            assert conn.is_authenticated

        async with pool.acquire() as conn:
            assert conn is first
            assert conn.token is None
            assert not conn.is_authenticated
            assert "invalidate" in servers[0].methods
            assert servers[0].identity is None
            if isinstance(conn, HTTPConnection):
                assert conn._signin_args is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_connection_whose_reset_fails_is_closed_and_not_reused(kind: str) -> None:
    servers: list[_FakeServer] = []
    pool = _pool(kind, servers, size=1)
    async with pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            first = conn
            await conn.authenticate(CALLER_TOKEN)
            servers[0].fail_signin = True

        assert not first.is_connected
        assert pool.available == 0
        assert pool.in_use == 0
        assert pool._semaphore._value == pool.size

        async with pool.acquire() as conn:
            assert conn is not first
            assert conn.token is not None and conn.token.startswith(f"token-for-{POOL_USER}")
            assert pool.in_use == 1
        assert pool.available == 1
        assert pool._semaphore._value == pool.size


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_set_credentials_closes_connections_that_cannot_sign_in(kind: str) -> None:
    servers: list[_FakeServer] = []
    async with _pool(kind, servers, size=2) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as a, pool.acquire() as b:
            pass
        assert pool.available == 2
        servers[0].fail_signin = True

        await pool.set_credentials("i391_new_user", "i391_new_pass")

        assert pool.available == 1
        assert not a.is_connected
        assert b.is_connected
        assert b.token is not None and b.token.startswith("token-for-i391_new_user")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_connection_released_after_pool_close_is_closed(kind: str) -> None:
    servers: list[_FakeServer] = []
    pool = _pool(kind, servers)
    await pool.set_credentials(POOL_USER, POOL_PASS)
    async with pool.acquire() as conn:
        await pool.close()
    assert not conn.is_connected
    assert pool.available == 0
    assert pool.in_use == 0


# ---------------------------------------------------------------------------
# Integration: $session on the pinned server
# ---------------------------------------------------------------------------

I_NS = "test"
I_DB = "i391_pool_auth"
ACCESS = "i391_account"


@pytest.fixture
async def record_token() -> AsyncGenerator[str, None]:
    """Define a record access on a fresh database and return a record user's token."""
    setup = HTTPConnection(URL, I_NS, I_DB)
    try:
        await setup.connect()
        await setup.signin("root", "root")
        await setup.query(f"DEFINE NAMESPACE IF NOT EXISTS {I_NS}; DEFINE DATABASE IF NOT EXISTS {I_DB};")
        await setup.query(
            f"""
            DEFINE TABLE OVERWRITE i391_user SCHEMALESS PERMISSIONS FULL;
            DEFINE ACCESS OVERWRITE {ACCESS} ON DATABASE TYPE RECORD
                SIGNUP (CREATE i391_user SET email = $email)
                SIGNIN (SELECT * FROM i391_user WHERE email = $email)
                DURATION FOR TOKEN 1h, FOR SESSION 1h;
            """
        )
        response = await setup.signup(I_NS, I_DB, ACCESS, email=f"{uuid.uuid4().hex}@example.com")
        assert response.token
        yield response.token
    finally:
        await setup.close()


async def _session(conn: BaseSurrealConnection) -> dict[str, Any]:
    response = await conn.query("RETURN $session")
    assert response.first_result is not None
    session = response.first_result.result
    assert isinstance(session, dict)
    return session


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("kind", KINDS)
async def test_reacquired_connection_carries_pool_identity(kind: str, record_token: str) -> None:
    async with ConnectionPool(URL, I_NS, I_DB, size=1, connection_type=kind) as pool:
        await pool.set_credentials("root", "root")
        async with pool.acquire() as conn:
            first = conn
            await conn.authenticate(record_token)
            caller = await _session(conn)
            assert caller["ac"] == ACCESS
            assert str(caller["rd"]).startswith("i391_user:")

        async with pool.acquire() as conn:
            assert conn is first
            session = await _session(conn)
            assert session["ac"] is None
            assert session["rd"] is None
            auth = await conn.query("RETURN $auth.id")
            assert auth.first_result is not None
            assert auth.first_result.result is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("kind", KINDS)
async def test_reacquired_connection_is_unauthenticated_without_pool_credentials(kind: str, record_token: str) -> None:
    async with ConnectionPool(URL, I_NS, I_DB, size=1, connection_type=kind) as pool:
        async with pool.acquire() as conn:
            first = conn
            await conn.authenticate(record_token)
            assert (await _session(conn))["ac"] == ACCESS

        async with pool.acquire() as conn:
            assert conn is first
            assert not conn.is_authenticated
            # The test server refuses unauthenticated sessions: the query fails
            # instead of running as the previous caller's record user.
            with pytest.raises(QueryError, match="(?i)anonymous"):
                await _session(conn)
