"""
Integration tests for the table-definition round trip, against a live server.

The unit tests pin the emitted SQL; only a real server can show that the
permissions actually land and that the state read back compares equal to the
model — which is what makes `makemigrations` converge. Before this fix the
server stored ``PERMISSIONS NONE`` for a model declaring a rule, because the
clause was emitted as a second ``DEFINE TABLE`` and rejected.

Run with: pytest -m integration tests/test_createtable_roundtrip_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector
from src.surreal_orm.migrations.introspector import introspect_models
from src.surreal_orm.migrations.operations import CreateTable
from src.surreal_orm.migrations.state import SchemaState, TableState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_createtable_roundtrip"
TABLE = "ct_guarded"


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


class Guarded(BaseSurrealModel):
    """A model that declares table permissions."""

    model_config = SurrealConfigDict(table_name=TABLE, permissions={"select": "$auth.id = id"})

    id: str | None = None
    n: int = 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_declared_permissions_reach_the_database() -> None:
    """They used to be silently dropped: the server stored PERMISSIONS NONE."""
    await Guarded.define_table()

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    info = await client.query("INFO FOR DB;")
    definition = info.first_result.result["tables"][TABLE]

    assert "FOR select WHERE $auth.id = id" in definition, definition


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_permissioned_table_diffs_to_nothing_once_applied() -> None:
    """The round trip has to converge, or makemigrations repeats itself forever."""
    await Guarded.define_table()

    current = await DatabaseIntrospector().introspect()
    operations = [
        op for op in current.diff(introspect_models([Guarded])) if getattr(op, "table", getattr(op, "name", None)) == TABLE
    ]

    assert operations == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redefining_a_table_is_accepted_by_the_server() -> None:
    """A plain second DEFINE TABLE was rejected as 'already exists'."""
    await Guarded.define_table()

    current = SchemaState()
    current.tables[TABLE] = TableState(name=TABLE, permissions={"select": "$auth.id = id"})
    target = SchemaState()
    target.tables[TABLE] = TableState(name=TABLE, permissions={"select": "true"})
    op = next(o for o in current.diff(target) if isinstance(o, CreateTable))

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(op.forwards())

    assert [r.status.value for r in response.results] == ["OK"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rolling_back_a_redefinition_keeps_the_rows() -> None:
    """The rollback used to be REMOVE TABLE, dropping every row."""
    await Guarded.define_table()
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(f"CREATE {TABLE}:keepme SET n = 7;")

    current = SchemaState()
    current.tables[TABLE] = TableState(name=TABLE, permissions={"select": "$auth.id = id"})
    target = SchemaState()
    target.tables[TABLE] = TableState(name=TABLE, permissions={"select": "true"})
    op = next(o for o in current.diff(target) if isinstance(o, CreateTable))

    await client.query(op.forwards())
    await client.query(op.backwards())

    rows = await client.query(f"SELECT * FROM {TABLE};")
    info = await client.query("INFO FOR DB;")

    assert len(rows.all_records) == 1, "the rollback destroyed the data"
    assert "FOR select WHERE $auth.id = id" in info.first_result.result["tables"][TABLE]
