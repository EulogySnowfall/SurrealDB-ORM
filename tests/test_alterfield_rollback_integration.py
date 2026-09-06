"""
Integration test for the rollback of an ``Encrypted`` column, on a live server.

The unit tests pin the emitted SQL. Only a real server shows the consequence:
after rolling back, does the column still hash what it stores? Before the fix
the rollback dropped ``VALUE crypto::argon2::generate($value)``, so it did not —
the password was written in plaintext.

Run with: pytest -m integration tests/test_alterfield_rollback_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.migrations.operations import AddField, AlterField, CreateTable
from src.surreal_orm.migrations.state import FieldState
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_alterfield_rollback"
TABLE = "af_users"


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    """Point the ORM at the test database."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


@pytest.fixture(autouse=True)
async def clean_database():
    """Drop the test table before and after each test."""

    async def cleanup() -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(f"REMOVE TABLE IF EXISTS {TABLE};")

    await cleanup()
    yield
    await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rolling_back_restores_password_hashing() -> None:
    """The column must still hash after the rollback, not store plaintext."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(CreateTable(name=TABLE).forwards())
    await client.query(AddField(table=TABLE, name="password", field_type="string", encrypted=True).forwards())

    encrypted = FieldState(name="password", field_type="string", encrypted=True)
    plain = FieldState(name="password", field_type="string")
    op = AlterField.from_field_states(TABLE, encrypted, plain)

    await client.query(op.forwards())
    await client.query(op.backwards())

    await client.query(f"CREATE {TABLE}:alice SET password = 'hunter2';")
    stored = (await client.query(f"SELECT password FROM {TABLE}:alice;")).all_records[0]["password"]

    assert stored != "hunter2", "the rollback dropped the VALUE clause — plaintext stored"
    assert stored.startswith("$argon2"), stored


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_forward_direction_really_drops_hashing() -> None:
    """Guards the test above: it must be the rollback doing the restoring.

    If ``forwards()`` also kept the VALUE clause, the assertion above would pass
    for the wrong reason.
    """
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(CreateTable(name=TABLE).forwards())
    await client.query(AddField(table=TABLE, name="password", field_type="string", encrypted=True).forwards())

    op = AlterField.from_field_states(
        TABLE,
        FieldState(name="password", field_type="string", encrypted=True),
        FieldState(name="password", field_type="string"),
    )
    await client.query(op.forwards())

    await client.query(f"CREATE {TABLE}:bob SET password = 'hunter2';")
    stored = (await client.query(f"SELECT password FROM {TABLE}:bob;")).all_records[0]["password"]

    assert stored == "hunter2"
