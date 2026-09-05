"""
Integration tests for the #168 backport, against a live SurrealDB 2.6.x.

Both halves were reproduced on 2.6.5 before porting:

- a plain ``DEFINE FIELD`` over an existing field answers
  ``The field 'a' already exists`` and leaves the definition untouched;
- that rejection rides back inside a **successful** HTTP/RPC response, so
  nothing downstream noticed.

Run with: pytest -m integration tests/test_issue_168_v2_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.migrations.executor import MigrationStatementError, _check_statements
from src.surreal_orm.migrations.operations import AddField, AlterField, CreateTable
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_168_v2"
TABLE = "i168_probe"


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
    """Drop the probe table before and after each test."""

    async def cleanup() -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(f"REMOVE TABLE IF EXISTS {TABLE};")

    await cleanup()
    yield
    await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_alter_field_actually_changes_the_column() -> None:
    """The reported defect: every AlterField was a silent no-op."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(CreateTable(name=TABLE).forwards())
    await client.query(AddField(table=TABLE, name="a", field_type="string").forwards())

    await client.query(AlterField(table=TABLE, name="a", field_type="int").forwards())

    info = await client.query(f"INFO FOR TABLE {TABLE};")
    definition = info.first_result.result["fields"]["a"]

    assert "TYPE int" in definition, definition


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_plain_redefinition_is_still_rejected_by_the_server() -> None:
    """Why OVERWRITE is required rather than merely tidier.

    Pinning the server behaviour the fix exists for: if a future SurrealDB made a
    plain re-DEFINE update the field, this test would start failing and tell us
    the workaround is no longer needed.
    """
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(CreateTable(name=TABLE).forwards())
    await client.query(AddField(table=TABLE, name="a", field_type="string").forwards())

    response = await client.query(f"DEFINE FIELD a ON {TABLE} TYPE int;")

    statuses = [r.status.value for r in response.results]
    assert "ERR" in statuses, statuses


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rejected_statement_no_longer_passes_for_success() -> None:
    """The executor half: the RPC succeeds, the statement did not."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(CreateTable(name=TABLE).forwards())
    await client.query(AddField(table=TABLE, name="a", field_type="string").forwards())

    sql = f"DEFINE FIELD a ON {TABLE} TYPE int;"
    response = await client.query(sql)  # a *successful* RPC carrying status: ERR

    with pytest.raises(MigrationStatementError, match="already exists"):
        _check_statements(response, sql, "migration 0001_probe")
