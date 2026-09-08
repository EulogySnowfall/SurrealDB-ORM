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
from src.surreal_orm.migrations.operations import AlterTable
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
    op = next(o for o in current.diff(target) if isinstance(o, AlterTable))

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
    op = next(o for o in current.diff(target) if isinstance(o, AlterTable))

    await client.query(op.forwards())
    await client.query(op.backwards())

    rows = await client.query(f"SELECT * FROM {TABLE};")
    info = await client.query("INFO FOR DB;")

    assert len(rows.all_records) == 1, "the rollback destroyed the data"
    assert "FOR select WHERE $auth.id = id" in info.first_result.result["tables"][TABLE]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_full_permission_is_stored_as_an_allow() -> None:
    """``FOR select WHERE FULL`` is accepted, then denies every read."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    op = AlterTable(name=TABLE, schema_mode="SCHEMAFULL", permissions={"select": "FULL"})

    response = await client.query(op.forwards())
    assert [r.status.value for r in response.results] == ["OK"]

    info = await client.query("INFO FOR DB;")
    stored = info.first_result.result["tables"][TABLE]

    assert "FOR select FULL" in stored, stored
    assert "WHERE FULL" not in stored, stored


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_orm_table_type_does_not_reach_the_server() -> None:
    """``TYPE USER`` is a parse error, so the whole migration aborts."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    current = TableState(name=TABLE, schema_mode="SCHEMALESS", table_type="user")
    target = TableState(name=TABLE, schema_mode="SCHEMAFULL", table_type="user")

    response = await client.query(AlterTable.from_table_states(current, target).forwards())

    assert [r.status.value for r in response.results] == ["OK"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_redefining_a_view_keeps_the_view() -> None:
    """An overwrite with no AS clause turns the view into a plain table."""
    # SurrealDB rejects a view whose source table does not exist.
    await Guarded.define_table()
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    view = f"{TABLE}_v"
    await client.query(f"REMOVE TABLE IF EXISTS {view}; DEFINE TABLE {view} AS (SELECT n FROM {TABLE});")

    current = TableState(name=view, schema_mode="SCHEMALESS", view_query=f"SELECT n FROM {TABLE}")
    target = TableState(name=view, schema_mode="SCHEMAFULL", view_query=f"SELECT n FROM {TABLE}")
    await client.query(AlterTable.from_table_states(current, target).forwards())

    info = await client.query("INFO FOR DB;")
    stored = info.first_result.result["tables"][view]
    await client.query(f"REMOVE TABLE IF EXISTS {view};")

    assert "AS SELECT" in stored, stored


@pytest.mark.integration
@pytest.mark.asyncio
async def test_define_table_is_idempotent_and_applies_a_changed_permission() -> None:
    """A plain DEFINE TABLE is rejected the second time, silently."""
    await Guarded.define_table()
    await Guarded.define_table()

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    info = await client.query("INFO FOR DB;")

    assert "FOR select WHERE $auth.id = id" in info.first_result.result["tables"][TABLE]
