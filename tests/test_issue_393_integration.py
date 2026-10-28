"""
#393 — a token is accepted only by the model whose access definition issued it.

Two record accesses on one table: the server accepts a token of either one, and
``$auth`` is a record of the shared table in both cases.  Each model must still
accept only the tokens of its own ``access_name``.
"""

from collections.abc import AsyncIterator

import pytest

from src import surreal_orm
from src.surreal_orm.auth import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, clear_model_registry
from src.surreal_orm.types import TableType
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_393"
TABLE = "I393User"
ACCESSES = ("i393_public", "i393_staff")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


@pytest.fixture(autouse=True)
async def _schema() -> AsyncIterator[None]:
    clear_model_registry()
    AuthenticatedUserMixin.invalidate_token_cache()
    AuthenticatedUserMixin.configure_token_cache(ttl=300)
    client = await surreal_orm.SurrealDBConnectionManager.reconnect()
    assert client is not None

    async def cleanup() -> None:
        for access in ACCESSES:
            await client.query(f"REMOVE ACCESS IF EXISTS {access} ON DATABASE;")
        await client.query(f"REMOVE TABLE IF EXISTS {TABLE};")

    await cleanup()
    await client.query(f"DEFINE TABLE {TABLE} SCHEMALESS;")
    for access in ACCESSES:
        await client.query(f"""
            DEFINE ACCESS {access} ON DATABASE TYPE RECORD
                SIGNUP (CREATE {TABLE} SET email = $email, password = crypto::argon2::generate($password))
                SIGNIN (SELECT * FROM {TABLE} WHERE email = $email AND crypto::argon2::compare(password, $password))
                DURATION FOR TOKEN 1h, FOR SESSION 1h;
        """)
    yield
    AuthenticatedUserMixin.invalidate_token_cache()
    await cleanup()


def _models() -> tuple[type, type]:
    class I393Public(AuthenticatedUserMixin, BaseSurrealModel):
        model_config = SurrealConfigDict(table_type=TableType.USER, table_name=TABLE, access_name="i393_public")
        id: str | None = None
        email: str
        password: Encrypted

    class I393Staff(AuthenticatedUserMixin, BaseSurrealModel):
        model_config = SurrealConfigDict(table_type=TableType.USER, table_name=TABLE, access_name="i393_staff")
        id: str | None = None
        email: str
        password: Encrypted

    return I393Public, I393Staff


async def test_token_of_each_access_is_accepted_only_by_its_own_model() -> None:
    public, staff = _models()
    _, public_token = await public.signup(email="i393@example.com", password="pw")
    # Same record, signed in through the other access.
    _, staff_token = await staff.signin(email="i393@example.com", password="pw")

    own_public = await public.validate_token(public_token)
    own_staff = await staff.validate_token(staff_token)
    assert own_public is not None and own_public.startswith(f"{TABLE}:")
    assert own_staff == own_public

    # Each own token is now cached for its model; neither entry serves the other.
    for use_cache in (True, False):
        assert await staff.validate_token(public_token, use_cache=use_cache) is None
        assert await public.validate_token(staff_token, use_cache=use_cache) is None
    assert await staff.authenticate_token(public_token) is None
    assert await public.authenticate_token(staff_token) is None

    # Own tokens still validate, from the cache and from the server.
    assert await public.validate_token(public_token) == own_public
    assert await staff.validate_token(staff_token, use_cache=False) == own_staff
    result = await staff.authenticate_token(staff_token)
    assert result is not None and result[1] == own_staff
