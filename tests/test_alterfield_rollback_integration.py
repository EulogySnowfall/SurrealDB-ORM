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
async def test_the_rollback_restores_password_hashing() -> None:
    """The column must still hash after the rollback, not store plaintext.

    One sequential test rather than two: the ``forwards()`` half is what proves
    the ``backwards()`` half is doing the restoring. Were the forward direction
    to keep the VALUE clause too, a separate rollback test would pass for the
    wrong reason.
    """
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(CreateTable(name=TABLE).forwards())
    await client.query(AddField(table=TABLE, name="password", field_type="string", encrypted=True).forwards())

    # nullable=False on both sides: the default leaves TYPE option<string> in
    # each direction, which hides a regression in the type half of the rollback.
    encrypted = FieldState(name="password", field_type="string", nullable=False, encrypted=True)
    plain = FieldState(name="password", field_type="string", nullable=False)
    op = AlterField.from_field_states(TABLE, encrypted, plain)

    # Forward: hashing is dropped, so a write stores what it was given.
    await client.query(op.forwards())
    await client.query(f"CREATE {TABLE}:bob SET password = 'hunter2';")
    forward_stored = (await client.query(f"SELECT password FROM {TABLE}:bob;")).all_records[0]["password"]

    assert forward_stored == "hunter2", "forwards() kept the VALUE clause; the rollback assertion below would be vacuous"

    # Rollback: hashing is back, and the type is the non-optional one.
    await client.query(op.backwards())
    await client.query(f"CREATE {TABLE}:alice SET password = 'hunter2';")
    stored = (await client.query(f"SELECT password FROM {TABLE}:alice;")).all_records[0]["password"]

    info = await client.query(f"INFO FOR TABLE {TABLE};")
    definition = info.first_result.result["fields"]["password"]

    assert stored != "hunter2", "the rollback dropped the VALUE clause — plaintext stored"
    assert stored.startswith("$argon2"), stored
    assert "TYPE string" in definition, definition
    assert "option<string>" not in definition, definition


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_default_survives_the_rollback_unquoted() -> None:
    """``backwards()`` single-quoted every DEFAULT, so a function became a string."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(CreateTable(name=TABLE).forwards())

    previous = FieldState(name="created", field_type="datetime", nullable=False, default="time::now()")
    target = FieldState(name="created", field_type="datetime", nullable=False)
    op = AlterField.from_field_states(TABLE, previous, target)

    await client.query(op.forwards())
    response = await client.query(op.backwards())

    assert [r.status.value for r in response.results] == ["OK"]

    info = await client.query(f"INFO FOR TABLE {TABLE};")
    definition = info.first_result.result["fields"]["created"]

    assert "DEFAULT time::now()" in definition, definition
    assert "DEFAULT 'time::now()'" not in definition, definition
