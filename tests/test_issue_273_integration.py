"""
Integration tests for issue #273 — the DEFINE parser reads SurrealQL as the server prints it.

A ``<`` or ``>`` comparison was counted as a bracket, so every clause after
``ASSERT $value > 0`` was folded into the assertion; a double-quoted table
permission condition holding an apostrophe opened a string that ran to the end
of the clause. Both left a schema that ``schema_diff()`` reported as changed on
every run. Each test defines the schema on the pinned server, introspects it,
and diffs it against a model declaring the same clauses.

Run with: pytest -m integration tests/test_issue_273_integration.py
"""

from collections.abc import AsyncIterator

import pytest

from src import surreal_orm
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.migrations.operations import Operation
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_273"

FIELDS_TABLE = "i273_fields"
PERMS_TABLE = "i273_perms"
TABLES = [FIELDS_TABLE, PERMS_TABLE]


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
async def clean_database() -> AsyncIterator[None]:
    """Drop every table of the test database before and after each test."""

    async def cleanup() -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        info = await client.query("INFO FOR DB;")
        for table in set(info.first_result.result["tables"]) | set(TABLES):
            await client.query(f"REMOVE TABLE IF EXISTS {table};")

    await cleanup()
    yield
    await cleanup()


async def _query(sql: str) -> None:
    """Run *sql* and fail on any statement the server rejects."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value == "ERR"]
    assert errors == [], f"{errors}\nSQL: {sql}"


def _operations_for(operations: list[Operation], table: str) -> list[Operation]:
    """Keep the operations touching *table*."""
    return [op for op in operations if (getattr(op, "table", None) or getattr(op, "name", None)) == table]


class I273Field(BaseSurrealModel):
    """Fields whose VALUE / ASSERT hold comparisons, casts and divisions."""

    model_config = SurrealConfigDict(table_name=FIELDS_TABLE, schema_mode="SCHEMAFULL")

    id: str | None = None
    n: int = 1
    s: str
    h: int


# The clauses the model cannot express are set on its introspected state,
# exactly as the database is defined below.
_DECLARED = {
    "n": {"value": "$value", "assertion": "$value > 0", "comment": "positive"},
    "s": {
        "value": "string::len($value) < 10",
        "assertion": "$value <= 'z' AND $value >= 'a'",
        "comment": "name",
    },
    "h": {"value": "$value / 2", "assertion": "$value = /^[0-9'(]+$/ OR $value < 10", "comment": "half"},
}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_field_with_comparison_in_assert_converges() -> None:
    await _query(
        f"DEFINE TABLE {FIELDS_TABLE} SCHEMAFULL;"
        f"DEFINE FIELD n ON {FIELDS_TABLE} TYPE int DEFAULT 1 VALUE $value ASSERT $value > 0 COMMENT 'positive';"
        f"DEFINE FIELD s ON {FIELDS_TABLE} TYPE string VALUE string::len($value) < 10 "
        "ASSERT $value <= 'z' AND $value >= 'a' COMMENT 'name';"
        f"DEFINE FIELD h ON {FIELDS_TABLE} TYPE int VALUE $value / 2 "
        "ASSERT $value = /^[0-9'(]+$/ OR $value < 10 COMMENT 'half';"
    )

    db_state = await DatabaseIntrospector().introspect()
    fields = db_state.tables[FIELDS_TABLE].fields
    for name, clauses in _DECLARED.items():
        assert (fields[name].value, fields[name].assertion, fields[name].comment) == (
            clauses["value"],
            clauses["assertion"],
            clauses["comment"],
        ), name

    model_state = ModelIntrospector([I273Field]).introspect()
    for name, clauses in _DECLARED.items():
        model_field = model_state.tables[FIELDS_TABLE].fields[name]
        model_field.value = clauses["value"]
        model_field.assertion = clauses["assertion"]
        model_field.comment = clauses["comment"]

    assert _operations_for(db_state.diff(model_state), FIELDS_TABLE) == []


class I273Perms(BaseSurrealModel):
    """A permission condition the server reports in double quotes."""

    model_config = SurrealConfigDict(
        table_name=PERMS_TABLE,
        schema_mode="SCHEMAFULL",
        comment="it's",
        permissions={
            "select": 'name = "it\'s"',
            "create": "NONE",
            "update": "NONE",
            "delete": "NONE",
        },
    )

    id: str | None = None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_table_permission_with_apostrophe_converges() -> None:
    await _query(
        f'DEFINE TABLE {PERMS_TABLE} TYPE NORMAL SCHEMAFULL COMMENT "it\'s" '
        'PERMISSIONS FOR select WHERE name = "it\'s", FOR create, update, delete NONE;'
    )

    db_state = await DatabaseIntrospector().introspect()
    assert db_state.tables[PERMS_TABLE].permissions == I273Perms.get_permissions()

    assert _operations_for(await schema_diff(models=[I273Perms]), PERMS_TABLE) == []
