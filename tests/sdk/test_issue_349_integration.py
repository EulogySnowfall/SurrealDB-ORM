"""Issue #349 — the HTTP 401 re-mint only replays the credentials of the current identity.

Against the pinned 2.x server: a connection signed in with ``signin()`` and then
switched to a short-lived record token with ``authenticate()`` must fail once that
token expires, rather than signing in again with the earlier credentials. Also
covers ``HTTPConnection.invalidate()``. The fixture removes its database afterwards.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest

from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.exceptions import QueryError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

NS = SURREALDB_NAMESPACE
DB = "i349_http_auth"
ACCESS = "i349_account"
TOKEN_SECONDS = 2


@pytest.fixture
async def root_conn() -> AsyncGenerator[HTTPConnection, None]:
    """A connection signed in with ``signin()``, with a record access whose tokens last 2 s."""
    conn = HTTPConnection(SURREALDB_URL, NS, DB)
    try:
        await conn.connect()
        await conn.signin(SURREALDB_USER, SURREALDB_PASS)
        await conn.query(f"DEFINE NAMESPACE IF NOT EXISTS {NS}; DEFINE DATABASE IF NOT EXISTS {DB};")
        response = await conn.query(
            f"""
            DEFINE TABLE OVERWRITE i349_user SCHEMALESS PERMISSIONS FULL;
            DEFINE ACCESS OVERWRITE {ACCESS} ON DATABASE TYPE RECORD
                SIGNUP (CREATE i349_user SET email = $email)
                SIGNIN (SELECT * FROM i349_user WHERE email = $email)
                DURATION FOR TOKEN {TOKEN_SECONDS}s, FOR SESSION 1h;
            """
        )
        assert response.is_ok, response.raw
        yield conn
    finally:
        try:
            await conn.close()
        finally:
            await _remove_database()


async def _remove_database() -> None:
    """Drop the test database through a fresh root connection (``conn`` may be signed out)."""
    cleanup = HTTPConnection(SURREALDB_URL, NS, DB)
    try:
        await cleanup.connect()
        await cleanup.signin(SURREALDB_USER, SURREALDB_PASS)
        response = await cleanup.query(f"REMOVE DATABASE IF EXISTS {DB};")
        assert response.is_ok, response.raw
    finally:
        await cleanup.close()


async def _record_token() -> str:
    """Sign up a fresh record user on a separate connection and return its token."""
    other = HTTPConnection(SURREALDB_URL, NS, DB)
    try:
        await other.connect()
        response = await other.signup(NS, DB, ACCESS, email=f"{uuid.uuid4().hex}@example.com")
        assert response.token
        return response.token
    finally:
        await other.close()


@pytest.mark.integration
async def test_expired_authenticated_token_fails_instead_of_signing_in_again(root_conn: HTTPConnection) -> None:
    token = await _record_token()
    await root_conn.authenticate(token)

    auth = await root_conn.query("RETURN $auth.id")
    assert auth.first_result is not None
    assert str(auth.first_result.result).startswith("i349_user:")

    await asyncio.sleep(TOKEN_SECONDS + 1.5)

    with pytest.raises(QueryError) as excinfo:
        await root_conn.query("INFO FOR ROOT")
    assert excinfo.value.code == 401
    assert root_conn.token == token  # still the record token, never replaced
    assert root_conn._signin_args is None


@pytest.mark.integration
async def test_invalidate_leaves_the_connection_unauthenticated(root_conn: HTTPConnection) -> None:
    info = await root_conn.query("INFO FOR ROOT")
    assert info.is_ok

    await root_conn.invalidate()

    assert root_conn.token is None
    assert not root_conn.is_authenticated
    assert root_conn._signin_args is None
    # 2.x reports the missing authentication as a permission error (not a 401).
    with pytest.raises(QueryError, match="(?i)not enough permissions") as excinfo:
        await root_conn.query("INFO FOR ROOT")
    assert excinfo.value.code == -32000
