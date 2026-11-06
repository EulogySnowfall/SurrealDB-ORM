"""
Integration tests for #568 on the v2 line, against a live SurrealDB 2.x server.

The definitions are created as an operator would write them, read back by
``DatabaseIntrospector`` from what ``INFO FOR ...`` reports, and diffed
against models describing the same schema. Before the fix, a comparison in a
``VALUE`` / ``ASSERT`` swallowed the ``COMMENT`` after it, and a double-quoted
``"it's"`` in a permission condition swallowed the remaining ``FOR`` groups,
so every diff reported these tables and fields as changed.

Run with: pytest -m integration tests/test_issue_568_v2_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector
from src.surreal_orm.migrations.introspector import introspect_models
from src.surreal_orm.migrations.operations import Operation
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_568_v2"
CMP = "i568_cmp"
PERM = "i568_perm"

_TABLE_PERMISSION = 'name = "it\'s" OR $auth.level > 1'


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
    """Drop the test tables before and after each test."""

    async def cleanup() -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(f"REMOVE TABLE IF EXISTS {CMP}; REMOVE TABLE IF EXISTS {PERM};")

    await cleanup()
    yield
    await cleanup()


async def _run(sql: str) -> None:
    """Execute *sql* and fail on any statement the server rejected.

    ``client.query()`` only raises on an RPC failure; a rejected statement
    comes back as ``status: ERR`` inside a successful response.
    """
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value != "OK"]
    assert errors == [], f"{errors}\nSQL: {sql}"


def _on(table: str, operations: list[Operation]) -> list[Operation]:
    return [op for op in operations if getattr(op, "table", getattr(op, "name", None)) == table]


class I568Cmp(BaseSurrealModel):
    """Fields whose VALUE / ASSERT hold a comparison, followed by a COMMENT."""

    model_config = SurrealConfigDict(table_name=CMP, schema_mode="SCHEMAFULL")

    id: str | None = None
    n: int = 1
    lt: int
    mix: int
    clamp: int


class I568Perm(BaseSurrealModel):
    """A table and a field permission holding a double-quoted apostrophe."""

    model_config = SurrealConfigDict(
        table_name=PERM,
        schema_mode="SCHEMAFULL",
        permissions={"select": _TABLE_PERMISSION, "create": "NONE", "update": "NONE", "delete": "NONE"},
    )

    id: str | None = None
    name: str
    e: str


#: What a model would declare, if v2 models could carry these clauses.
_CMP_CLAUSES = {
    "n": {"value": "$value", "assertion": "$value > 0", "comment": "positive"},
    "lt": {"assertion": "$value < 10", "comment": "lt"},
    "mix": {"assertion": "$value <= 10 AND $value >= 1 AND $value != 5", "comment": "mix"},
    "clamp": {"value": "IF $value > 5 { 5 } ELSE { $value }", "comment": "clamp"},
}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_comparisons_keep_the_comment_and_converge() -> None:
    """Each clause reads back on its own, and a matching model diffs to nothing."""
    await _run(
        f"DEFINE TABLE {CMP} SCHEMAFULL; "
        f"DEFINE FIELD n ON {CMP} TYPE int DEFAULT 1 VALUE $value ASSERT $value > 0 COMMENT 'positive'; "
        f"DEFINE FIELD lt ON {CMP} TYPE int ASSERT $value < 10 COMMENT 'lt'; "
        f"DEFINE FIELD mix ON {CMP} TYPE int ASSERT $value <= 10 AND $value >= 1 AND $value != 5 COMMENT 'mix'; "
        f"DEFINE FIELD clamp ON {CMP} TYPE int VALUE IF $value > 5 {{ 5 }} ELSE {{ $value }} COMMENT 'clamp';"
    )

    db_state = await DatabaseIntrospector().introspect()
    fields = db_state.tables[CMP].fields
    for name, clauses in _CMP_CLAUSES.items():
        for attr, expected in clauses.items():
            assert getattr(fields[name], attr) == expected, (name, attr)
    assert fields["n"].default == 1

    model_state = introspect_models([I568Cmp])
    for name, clauses in _CMP_CLAUSES.items():
        for attr, expected in clauses.items():
            setattr(model_state.tables[CMP].fields[name], attr, expected)

    assert _on(CMP, db_state.diff(model_state)) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_double_quoted_permission_strings_converge() -> None:
    """Table and field permissions read back one entry per action; schema_diff() is empty."""
    await _run(
        f"DEFINE TABLE {PERM} SCHEMAFULL PERMISSIONS FOR select WHERE {_TABLE_PERMISSION}, "
        f"FOR create, update, delete NONE; "
        f"DEFINE FIELD name ON {PERM} TYPE string; "
        f'DEFINE FIELD e ON {PERM} TYPE string PERMISSIONS FOR select WHERE name = "it\'s", FOR create, update NONE;'
    )

    table = (await DatabaseIntrospector().introspect()).tables[PERM]
    assert table.permissions == {
        "select": _TABLE_PERMISSION,
        "create": "NONE",
        "update": "NONE",
        "delete": "NONE",
    }
    assert table.fields["e"].permissions == {"select": 'name = "it\'s"', "create": "NONE", "update": "NONE"}

    assert _on(PERM, await schema_diff(models=[I568Perm])) == []
