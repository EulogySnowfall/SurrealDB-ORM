"""
Integration tests for #366 on the v2 line, against a live SurrealDB 2.x server.

The SIGNUP clause assigns only the sign-up set (identifier, password,
``signup_fields`` entries, ``created_at`` when the model has it).  These tests
apply the schema the migration path generates for a USER model and measure,
on the pinned 2.x server, that a field left out of the clause takes its
schema ``DEFAULT`` and that a sign-up request carrying an unlisted field does
not write it.

Run with: pytest -m integration tests/test_issue_366_v2_integration.py
"""

import uuid
from collections.abc import AsyncGenerator

import pytest

from src import surreal_orm
from src.surreal_orm.auth import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.migrations.state import SchemaState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, SurrealDbError
from src.surreal_orm.types import SchemaMode, TableType
from src.surreal_sdk.connection.http import HTTPConnection
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_366_v2"
FULL_TABLE = "i366_user"
FULL_ACCESS = "i366_account"
LESS_TABLE = "i366_loose_user"
LESS_ACCESS = "i366_loose_account"

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class I366User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name=FULL_TABLE,
        schema_mode=SchemaMode.SCHEMAFULL,
        access_name=FULL_ACCESS,
        signup_fields=["name"],
    )
    id: str | None = None
    email: str
    password: Encrypted
    name: str = ""
    role: str = "user"


class I366LooseUser(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name=LESS_TABLE,
        schema_mode=SchemaMode.SCHEMALESS,
        access_name=LESS_ACCESS,
    )
    id: str | None = None
    email: str
    password: Encrypted
    role: str = "user"


async def _remove_all() -> None:
    """Drop both tables and their access methods, under either name."""
    client = await surreal_orm.SurrealDBConnectionManager.reconnect()
    assert client is not None
    for table, access in ((FULL_TABLE, FULL_ACCESS), (LESS_TABLE, LESS_ACCESS)):
        await client.query(
            f"REMOVE ACCESS IF EXISTS {access} ON DATABASE;"
            f"REMOVE ACCESS IF EXISTS {table}_auth ON DATABASE;"
            f"REMOVE TABLE IF EXISTS {table};"
        )


@pytest.fixture
async def schema() -> AsyncGenerator[None, None]:
    """Apply the migration-generated schema of both models to a clean database."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    await _remove_all()
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    target = ModelIntrospector([I366User, I366LooseUser]).introspect()
    for operation in SchemaState().diff(target):
        response = await client.query(operation.forwards())
        for statement in response.results:
            assert statement.is_ok, f"{operation.forwards()!r}: {statement.result!r}"
    try:
        yield
    finally:
        await _remove_all()


async def _stored(table: str, email: str) -> dict[str, object]:
    """Read the stored record as root, bypassing the model."""
    client = await surreal_orm.SurrealDBConnectionManager.reconnect()
    assert client is not None
    result = await client.query(f"SELECT * FROM {table} WHERE email = $email;", {"email": email})
    assert not result.is_empty
    record: dict[str, object] = result.first
    return record


async def _raw_signup(access: str, **fields: object) -> None:
    """Sign up through a bare SDK connection, without the model's keyword check."""
    conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    try:
        await conn.connect()
        response = await conn.signup(SURREALDB_NAMESPACE, SURREALDB_DATABASE, access, **fields)
        assert response.token
    finally:
        await conn.close()


async def test_signup_without_optional_fields_keeps_the_defaults(schema: None) -> None:
    email = f"{uuid.uuid4().hex}@example.com"

    user, token = await I366User.signup(email=email, password="secret")

    assert token
    assert user.role == "user"
    assert user.name == ""
    stored = await _stored(FULL_TABLE, email)
    assert stored["role"] == "user"
    assert stored["name"] == ""
    assert stored["password"] != "secret"


async def test_signup_with_a_listed_field_sets_it(schema: None) -> None:
    email = f"{uuid.uuid4().hex}@example.com"

    user, _ = await I366User.signup(email=email, password="secret", name="Alice")

    assert user.name == "Alice"
    assert user.role == "user"
    stored = await _stored(FULL_TABLE, email)
    assert stored["name"] == "Alice"
    assert stored["role"] == "user"


async def test_raw_signup_request_cannot_set_an_unlisted_field(schema: None) -> None:
    """A sign-up request that does not go through the model still only sets the sign-up set."""
    email = f"{uuid.uuid4().hex}@example.com"

    await _raw_signup(FULL_ACCESS, email=email, password="secret", name="Bob", role="editor")

    stored = await _stored(FULL_TABLE, email)
    assert stored["role"] == "user"
    assert stored["name"] == "Bob"


async def test_raw_signup_on_a_schemaless_table_keeps_the_default(schema: None) -> None:
    """On a SCHEMALESS table an unlisted field is not stored either; the schema DEFAULT applies."""
    email = f"{uuid.uuid4().hex}@example.com"

    await _raw_signup(LESS_ACCESS, email=email, password="secret", role="editor", extra="x")

    stored = await _stored(LESS_TABLE, email)
    assert stored["role"] == "user"
    assert "extra" not in stored


async def test_signup_rejects_an_unlisted_keyword(schema: None) -> None:
    email = f"{uuid.uuid4().hex}@example.com"

    with pytest.raises(SurrealDbError, match="signup_fields"):
        await I366User.signup(email=email, password="secret", role="editor")

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    result = await client.query(f"SELECT * FROM {FULL_TABLE} WHERE email = $email;", {"email": email})
    assert result.is_empty
