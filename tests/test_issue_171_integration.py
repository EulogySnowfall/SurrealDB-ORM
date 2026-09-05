"""
Integration tests for issue #171 — ``makemigrations`` diffed against an empty schema.

The CLI plumbing (the ``--from-db`` flag, the error path, the empty-state fallback)
is covered by ``tests/test_cli.py`` with a mocked database state. What only a live
server can prove is the invariant the fix now depends on: the state read back from
SurrealDB compares **equal** to the state introspected from the models, so an
already-migrated schema produces no operations.

That equality is exactly what #170 had to fix first. While foreign keys introspected
as ``any`` and virtual fields produced columns, diffing against the database would
have generated spurious operations on every run — a worse bug than the one #171
reports.

Run with: pytest -m integration tests/test_issue_171_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.fields import ForeignKey, ManyToMany
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector
from src.surreal_orm.migrations.introspector import introspect_models
from src.surreal_orm.migrations.operations import AddField, AlterField, CreateTable
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
)
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_171"

TABLES = ["i171_articles", "i171_authors"]


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
        for table in TABLES:
            await client.query(f"REMOVE TABLE IF EXISTS {table};")

    await cleanup()
    yield
    await cleanup()


class I171Author(BaseSurrealModel):
    """Target of the foreign key — a record link is the case #170 had to fix."""

    model_config = SurrealConfigDict(table_name="i171_authors")

    id: str | None = None
    name: str


class I171Article(BaseSurrealModel):
    """Carries an optional scalar, a record link and a virtual graph relation."""

    model_config = SurrealConfigDict(table_name="i171_articles")

    id: str | None = None
    title: str
    subtitle: str | None = None
    author: ForeignKey("I171Author")
    shelves: ManyToMany("I171Author")


def _operations_for(operations: list, *tables: str) -> list:
    """Keep only the operations touching the tables under test.

    The test database is shared, so other tables produce their own operations.
    """
    kept = []
    for op in operations:
        table = getattr(op, "table", None) or getattr(op, "name", None)
        if table in tables:
            kept.append(op)
    return kept


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_migrated_schema_diffs_to_nothing() -> None:
    """The reported symptom: every run re-created every table.

    This is the comparison ``makemigrations`` now performs — the database state
    against the model state — so an empty result is the fix.
    """
    await I171Author.define_table()
    await I171Article.define_table()

    current = await DatabaseIntrospector().introspect()
    desired = introspect_models([I171Author, I171Article])

    assert _operations_for(current.diff(desired), *TABLES) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_state_re_emits_everything() -> None:
    """The old behaviour, kept as the ``--no-from-db`` escape hatch.

    Diffing against nothing must still produce the full schema, otherwise a first
    migration would generate an empty file.
    """
    from src.surreal_orm.migrations.state import SchemaState

    operations = SchemaState().diff(introspect_models([I171Author, I171Article]))
    relevant = _operations_for(operations, *TABLES)

    assert any(isinstance(op, CreateTable) for op in relevant)
    assert any(isinstance(op, AddField) and op.name == "title" for op in relevant)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_real_model_change_is_still_detected() -> None:
    """A column the database does not have must still produce an operation."""
    await I171Author.define_table()
    await I171Article.define_table()

    clear_model_registry()

    class I171ArticleChanged(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="i171_articles")
        id: str | None = None
        title: str
        subtitle: str | None = None
        author: ForeignKey("I171Author")
        shelves: ManyToMany("I171Author")
        published: bool = False  # new column

    current = await DatabaseIntrospector().introspect()
    operations = _operations_for(current.diff(introspect_models([I171ArticleChanged])), "i171_articles")

    assert [op for op in operations if isinstance(op, (AddField, AlterField)) and op.name == "published"]
