"""
Integration tests for issue #354 on the pinned server: the migration generated
for a ``TableType.USER`` model makes the identifier field unique, and a
migrated database diffs to nothing.
"""

import uuid
from collections.abc import AsyncGenerator

import pytest

from src import surreal_orm
from src.surreal_orm.auth import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.executor import _check_statements
from src.surreal_orm.migrations.operations import Operation
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, SurrealDbError
from src.surreal_orm.types import SchemaMode, TableType
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "i354_unique_identifier"
TABLE = "i354_account_user"
ACCESS = "i354_account"

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class I354AccountUser(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name=TABLE,
        schema_mode=SchemaMode.SCHEMAFULL,
        access_name=ACCESS,
    )
    id: str | None = None
    email: str
    password: Encrypted


def _own(operations: list[Operation]) -> list[Operation]:
    """Keep the operations about this file's table and access; the database is shared."""
    return [
        op
        for op in operations
        if TABLE in {getattr(op, "table", None), getattr(op, "name", None)} or getattr(op, "name", None) == ACCESS
    ]


async def _drop() -> None:
    client = await surreal_orm.SurrealDBConnectionManager.reconnect()
    assert client is not None
    await client.query(f"REMOVE ACCESS IF EXISTS {ACCESS} ON DATABASE; REMOVE TABLE IF EXISTS {TABLE};")


async def _apply(operations: list[Operation]) -> None:
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    for op in operations:
        sql = op.forwards()
        _check_statements(await client.query(sql), sql, op.describe())


@pytest.fixture
async def migrated() -> AsyncGenerator[None, None]:
    """Apply the migration ``schema_diff`` generates for the model to a clean database."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    await _drop()
    await _apply(_own(await schema_diff([I354AccountUser])))
    try:
        yield
    finally:
        await _drop()


async def test_second_signup_with_the_same_identifier_fails(migrated: None) -> None:
    email = f"{uuid.uuid4().hex}@example.com"
    await I354AccountUser.signup(email=email, password="first")

    with pytest.raises(SurrealDbError, match="Signup failed"):
        await I354AccountUser.signup(email=email, password="second")

    client = await surreal_orm.SurrealDBConnectionManager.reconnect()
    assert client is not None
    result = await client.query(f"SELECT count() AS n FROM {TABLE} WHERE email = $email GROUP ALL;", {"email": email})
    assert result.first == {"n": 1}


async def test_a_different_identifier_still_signs_up(migrated: None) -> None:
    await I354AccountUser.signup(email=f"{uuid.uuid4().hex}@example.com", password="first")
    user, token = await I354AccountUser.signup(email=f"{uuid.uuid4().hex}@example.com", password="second")

    assert token
    assert user.get_id()


async def test_schema_diff_is_empty_after_the_migration(migrated: None) -> None:
    assert _own(await schema_diff([I354AccountUser])) == []


async def test_existing_unique_index_under_another_name_is_kept(migrated: None) -> None:
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(
        f"REMOVE INDEX {TABLE}_email_unique ON {TABLE}; DEFINE INDEX legacy_email_idx ON {TABLE} FIELDS email UNIQUE;"
    )

    assert _own(await schema_diff([I354AccountUser])) == []
