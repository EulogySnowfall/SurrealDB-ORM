"""
Integration tests for issue #333 on the pinned server: the SIGNUP clause
assigns only the sign-up set, and every other field keeps its default.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest

from src import surreal_orm
from src.surreal_orm.auth import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, SurrealDbError
from src.surreal_orm.types import SchemaMode, TableType
from src.surreal_sdk.connection.http import HTTPConnection
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "i333_signup"
TABLE = "i333_user"
ACCESS = "i333_account"

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class I333User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name=TABLE,
        schema_mode=SchemaMode.SCHEMAFULL,
        access_name=ACCESS,
        signup_fields=["name"],
    )
    id: str | None = None
    email: str
    password: Encrypted
    name: str = ""
    role: str = "user"


@pytest.fixture
async def user_model() -> AsyncGenerator[type[I333User], None]:
    """Apply the table and access definition of ``I333User`` to a clean database."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(f"REMOVE ACCESS IF EXISTS {ACCESS} ON DATABASE; REMOVE TABLE IF EXISTS {TABLE};")
    await I333User.define_table()
    await I333User.define_access()
    try:
        yield I333User
    finally:
        client = await surreal_orm.SurrealDBConnectionManager.reconnect()
        if client is not None:
            await client.query(f"REMOVE ACCESS IF EXISTS {ACCESS} ON DATABASE; REMOVE TABLE IF EXISTS {TABLE};")


async def _stored(email: str) -> dict[str, object]:
    """Read the stored record as root, bypassing the model."""
    client = await surreal_orm.SurrealDBConnectionManager.reconnect()
    assert client is not None
    result = await client.query(f"SELECT * FROM {TABLE} WHERE email = $email;", {"email": email})
    assert not result.is_empty
    record: dict[str, object] = result.first
    return record


async def test_signup_without_role_keeps_the_default(user_model: type[I333User]) -> None:
    email = f"{uuid.uuid4().hex}@example.com"

    user, token = await user_model.signup(email=email, password="secret", name="Alice")

    assert token
    assert user.role == "user"
    assert user.name == "Alice"
    stored = await _stored(email)
    assert stored["role"] == "user"
    assert stored["name"] == "Alice"
    assert stored["password"] != "secret"


async def test_raw_signup_request_cannot_set_an_unlisted_field(user_model: type[I333User]) -> None:
    """A sign-up request that does not go through the model still only sets the sign-up set."""
    email = f"{uuid.uuid4().hex}@example.com"
    conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    try:
        await conn.connect()
        response = await conn.signup(
            SURREALDB_NAMESPACE,
            SURREALDB_DATABASE,
            ACCESS,
            email=email,
            password="secret",
            name="Bob",
            role="editor",
        )
        assert response.token
    finally:
        await conn.close()

    stored = await _stored(email)
    assert stored["role"] == "user"
    assert stored["name"] == "Bob"


async def test_signup_rejects_an_unlisted_keyword(user_model: type[I333User]) -> None:
    email = f"{uuid.uuid4().hex}@example.com"

    with pytest.raises(SurrealDbError, match="signup_fields"):
        await user_model.signup(email=email, password="secret", role="editor")

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    result = await client.query(f"SELECT * FROM {TABLE} WHERE email = $email;", {"email": email})
    assert result.is_empty
