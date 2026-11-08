"""
Integration test for issue #371 on the pinned server: ``schema_diff()`` reads an
access definition written before 0.35.0 (every field a ``$param``) and replaces
it with the sign-up set of ``build_signup_fields()``, then converges.

The unit test of #333 built the existing ``AccessState`` by hand; this one goes
through ``INFO FOR DB`` and ``parse_define_access()``, so a mismatch between the
parser and the builder (whitespace, field order) shows up as a second diff.
"""

from collections.abc import AsyncGenerator

import pytest

from src import surreal_orm
from src.surreal_orm.auth import AuthenticatedUserMixin
from src.surreal_orm.auth.access import build_signup_fields
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.operations import DefineAccess, RemoveAccess
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.types import SchemaMode, TableType
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "i371_signup_diff"
TABLE = "i371_user"
ACCESS = "i371_account"

#: The definition ``define_access()`` applied before 0.35.0: every model field
#: assigned from a parameter of the same name.
PRE_035_ACCESS = (
    f"DEFINE ACCESS {ACCESS} ON DATABASE TYPE RECORD"
    f" SIGNUP (CREATE {TABLE} SET email = $email, password = crypto::argon2::generate($password),"
    " name = $name, role = $role, is_active = $is_active)"
    f" SIGNIN (SELECT * FROM {TABLE} WHERE email = $email AND crypto::argon2::compare(password, $password))"
    " DURATION FOR TOKEN 15m, FOR SESSION 12h;"
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class I371User(AuthenticatedUserMixin, BaseSurrealModel):
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
    name: str
    role: str = "user"
    is_active: bool = True


@pytest.fixture
async def client() -> AsyncGenerator[object, None]:
    """A clean database holding the table and the pre-0.35.0 access definition."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    conn = await surreal_orm.SurrealDBConnectionManager.get_client()
    await conn.query(f"REMOVE ACCESS IF EXISTS {ACCESS} ON DATABASE; REMOVE TABLE IF EXISTS {TABLE};")
    await I371User.define_table()
    result = await conn.query(PRE_035_ACCESS)
    assert all(item.get("status") == "OK" for item in result.raw), result.raw
    try:
        yield conn
    finally:
        conn = await surreal_orm.SurrealDBConnectionManager.reconnect()
        if conn is not None:
            await conn.query(f"REMOVE ACCESS IF EXISTS {ACCESS} ON DATABASE; REMOVE TABLE IF EXISTS {TABLE};")


def _access_operations(operations: list[object]) -> list[object]:
    return [op for op in operations if isinstance(op, RemoveAccess | DefineAccess)]


async def test_schema_diff_replaces_a_pre_035_access_and_converges(client: object) -> None:
    operations = await schema_diff([I371User])

    access_ops = _access_operations(list(operations))
    assert [type(op) for op in access_ops] == [RemoveAccess, DefineAccess]
    remove, define = access_ops
    assert isinstance(remove, RemoveAccess) and isinstance(define, DefineAccess)
    assert remove.name == ACCESS
    assert define.name == ACCESS
    assert define.signup_fields == build_signup_fields(I371User)
    assert define.signup_fields == {
        "email": "$email",
        "password": "crypto::argon2::generate($password)",
        "name": "$name",
    }

    # Apply the whole diff (the identifier's UNIQUE index comes with it), as
    # makemigrations + migrate would.
    conn = await surreal_orm.SurrealDBConnectionManager.get_client()
    for op in operations:
        result = await conn.query(op.forwards())
        assert all(item.get("status") == "OK" for item in result.raw), (op.describe(), result.raw)

    second = await schema_diff([I371User])
    assert _access_operations(list(second)) == []
    assert second == []
