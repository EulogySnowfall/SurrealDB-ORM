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

import asyncio
import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import anyio
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
        self.rejected_tokens: set[str] = set()
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
            if params[0] in self.rejected_tokens:
                return RPCResponse(id=request.id or 0, error=RPCError(code=-32000, message="token rejected"))
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
        # A real round trip suspends the caller (and is where a cancellation
        # is delivered).
        await asyncio.sleep(0)
        if isinstance(conn, HTTPConnection):
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
            await asyncio.sleep(0)
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
            response = await conn.query("RETURN $auth")
            assert response.first_result is not None
            identity = response.first_result.result
            assert identity is not None and "signup" not in str(identity)


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
            if isinstance(conn, HTTPConnection):
                # Stateless: dropping the token locally is the whole reset.
                assert conn._signin_args is None
                assert "invalidate" not in servers[0].methods
            else:
                assert "invalidate" in servers[0].methods
            response = await conn.query("RETURN $auth")
            assert response.first_result is not None
            assert response.first_result.result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_connection_whose_reset_fails_is_closed_and_not_reused(kind: str) -> None:
    """Credentials changed while the connection was out, and it cannot sign in with the new ones."""
    servers: list[_FakeServer] = []
    pool = _pool(kind, servers, size=1)
    async with pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            first = conn
            await conn.authenticate(CALLER_TOKEN)
            servers[0].fail_signin = True
            await pool.set_credentials("i391_new_user", "i391_new_pass")

        assert not first.is_connected
        assert pool.available == 0
        assert pool.in_use == 0
        assert pool._semaphore._value == pool.size
        assert first not in pool._pool_auth

        async with pool.acquire() as conn:
            assert conn is not first
            assert conn.token is not None and conn.token.startswith("token-for-i391_new_user")
            assert pool.in_use == 1
        assert pool.available == 1
        assert pool._semaphore._value == pool.size


@pytest.mark.asyncio
async def test_websocket_rejected_pool_token_falls_back_to_signin() -> None:
    servers: list[_FakeServer] = []
    async with _pool("websocket", servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            first = conn
            stale = conn.token
            assert stale is not None
            servers[0].rejected_tokens.add(stale)

        async with pool.acquire() as conn:
            assert conn is first
            assert conn.token is not None and conn.token != stale
            assert conn.token.startswith(f"token-for-{POOL_USER}")
            assert servers[0].signins == 2


@pytest.mark.asyncio
async def test_websocket_rejected_pool_token_and_failed_signin_close_the_connection() -> None:
    servers: list[_FakeServer] = []
    async with _pool("websocket", servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            first = conn
            await conn.authenticate(CALLER_TOKEN)
            servers[0].rejected_tokens.add(pool._pool_auth[conn][0])
            servers[0].fail_signin = True

        assert not first.is_connected
        assert pool.available == 0
        assert pool.in_use == 0
        assert pool._semaphore._value == pool.size


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_release_puts_pool_token_back_without_signing_in(kind: str) -> None:
    """The reset reuses the pool's token: no ``signin`` per release."""
    servers: list[_FakeServer] = []
    async with _pool(kind, servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        for _ in range(3):
            await pool.query("RETURN 1")
        async with pool.acquire() as conn:
            await conn.authenticate(CALLER_TOKEN)
        async with pool.acquire() as conn:
            assert conn.token is not None and conn.token.startswith(f"token-for-{POOL_USER}")
        assert servers[0].signins == 1
        if kind == "http":
            # Stateless: the release sends nothing at all.
            assert servers[0].methods == ["query"] * 3 + ["authenticate"]
        else:
            assert servers[0].methods.count("authenticate") == 6


@pytest.mark.asyncio
async def test_http_keeps_a_token_re_minted_with_the_pool_credentials() -> None:
    """A token re-minted for the pool's identity (a transient 401) replaces the stored one."""
    servers: list[_FakeServer] = []
    async with _pool("http", servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            await conn.signin(POOL_USER, POOL_PASS)
            fresh = conn.token
        async with pool.acquire() as conn:
            assert conn.token == fresh
        assert servers[0].signins == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_connection_in_use_during_set_credentials_takes_the_new_ones(kind: str) -> None:
    servers: list[_FakeServer] = []
    async with _pool(kind, servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        async with pool.acquire() as conn:
            await pool.set_credentials("i391_new_user", "i391_new_pass")
        async with pool.acquire() as conn:
            assert conn.token is not None and conn.token.startswith("token-for-i391_new_user")


def _hold_next_signin(conn: BaseSurrealConnection) -> tuple[asyncio.Event, asyncio.Event]:
    """
    Make *conn*'s next ``signin()`` pause once the server has answered.

    Returns ``(answered, resume)``: ``answered`` is set when the token is back,
    and the call returns only once ``resume`` is set.
    """
    answered, resume = asyncio.Event(), asyncio.Event()
    real = conn.signin

    async def held(*args: Any, **kwargs: Any) -> Any:
        conn.signin = real  # type: ignore[method-assign]
        response = await real(*args, **kwargs)
        answered.set()
        await resume.wait()
        return response

    conn.signin = held  # type: ignore[method-assign]
    return answered, resume


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_credentials_changed_while_first_signin_is_pending(kind: str) -> None:
    """New credentials set while a new connection signs in are the ones it ends up with."""
    servers: list[_FakeServer] = []
    pool = _pool(kind, servers)
    create = pool._create_connection
    held: list[tuple[asyncio.Event, asyncio.Event]] = []

    def create_held() -> BaseSurrealConnection:
        conn = create()
        held.append(_hold_next_signin(conn))
        return conn

    pool._create_connection = create_held  # type: ignore[method-assign]
    async with pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)

        async def use() -> None:
            async with pool.acquire():
                pass

        user = asyncio.create_task(use())
        while not held:
            await asyncio.sleep(0)
        answered, resume = held[0]
        await answered.wait()
        change = asyncio.create_task(pool.set_credentials("i391_new_user", "i391_new_pass"))
        await asyncio.sleep(0.01)
        resume.set()
        await user
        await change

        async with pool.acquire() as conn:
            assert conn.token is not None and conn.token.startswith("token-for-i391_new_user")
            if kind == "http":
                assert isinstance(conn, HTTPConnection)
                assert conn._signin_args is not None and conn._signin_args["user"] == "i391_new_user"
        assert servers[0].identity is not None and "i391_new_user" in servers[0].identity


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_credentials_changed_while_release_signin_is_pending(kind: str) -> None:
    """New credentials set while a released connection signs in again are the ones it ends up with."""
    servers: list[_FakeServer] = []
    async with _pool(kind, servers) as pool:
        await pool.set_credentials(POOL_USER, POOL_PASS)
        answered = resume = asyncio.Event()
        change: asyncio.Task[None] | None = None

        async def use() -> None:
            nonlocal answered, resume
            async with pool.acquire() as conn:
                # The release has to sign in: the pool's token is now stale.
                await pool.set_credentials("i391_mid_user", "i391_mid_pass")
                answered, resume = _hold_next_signin(conn)

        user = asyncio.create_task(use())
        while not answered.is_set():
            await asyncio.sleep(0)
        change = asyncio.create_task(pool.set_credentials("i391_new_user", "i391_new_pass"))
        await asyncio.sleep(0.01)
        resume.set()
        await user
        await change

        async with pool.acquire() as conn:
            assert conn.token is not None and conn.token.startswith("token-for-i391_new_user")
        assert servers[0].identity is not None and "i391_new_user" in servers[0].identity


@pytest.mark.asyncio
async def test_credentials_set_while_release_invalidates_are_the_ones_handed_out() -> None:
    """Credentials set while a release without credentials invalidates the session apply to the next caller."""
    servers: list[_FakeServer] = []
    async with _pool("websocket", servers) as pool:
        reached, resume = asyncio.Event(), asyncio.Event()

        async def use() -> None:
            async with pool.acquire() as conn:
                await conn.authenticate(CALLER_TOKEN)
                real = conn._send_rpc

                async def held(request: RPCRequest) -> RPCResponse:
                    if str(request.method) == "invalidate":
                        reached.set()
                        await resume.wait()
                    return await real(request)

                conn._send_rpc = held  # type: ignore[method-assign]

        user = asyncio.create_task(use())
        await reached.wait()
        # The connection is still in use: set_credentials() leaves it to the release.
        await pool.set_credentials(POOL_USER, POOL_PASS)
        resume.set()
        await user
        assert pool.in_use == 0 and pool.available == 1

        async with pool.acquire() as conn:
            assert conn.is_authenticated
            assert conn.token is not None and conn.token.startswith(f"token-for-{POOL_USER}")
        assert servers[0].identity is not None and POOL_USER in servers[0].identity
        assert servers[0].methods[-1] != "invalidate"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("credentials", [True, False])
@pytest.mark.parametrize("change", [False, True])
async def test_cancelled_release_keeps_pool_accounting(kind: str, credentials: bool, change: bool) -> None:
    """A release cancelled by an enclosing scope gives the permit back and leaves nothing in use."""
    servers: list[_FakeServer] = []
    async with _pool(kind, servers, size=2) as pool:
        if credentials:
            await pool.set_credentials(POOL_USER, POOL_PASS)
        for _ in range(pool.size + 1):
            with anyio.CancelScope() as scope:
                async with pool.acquire() as conn:
                    if change:
                        await conn.authenticate(CALLER_TOKEN)
                    scope.cancel()
                    await anyio.sleep(0)
            assert scope.cancelled_caught
            assert pool.in_use == 0
            assert pool._semaphore._value == pool.size
        # Still usable: a permit never given back would make this block forever.
        with anyio.fail_after(5):
            async with pool.acquire() as a, pool.acquire() as b:
                assert a is not b
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
