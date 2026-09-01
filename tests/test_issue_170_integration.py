"""
Integration tests for issue #170 — relation markers, virtual fields and
nullability in generated DDL.

Verifies that the DDL the introspector now produces is accepted by SurrealDB
and that ``REFERENCE ON DELETE`` is actually enforced by the server.

Run with: pytest -m integration tests/test_issue_170_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.fields import ForeignKey, ManyToMany, Relation
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
)
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_170"

TABLES = ["i170_authors", "i170_posts", "i170_groups", "i170_members"]


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    """Setup SurrealDB connection for tests."""
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

    clear_model_registry()
    await cleanup()
    yield
    await cleanup()
    clear_model_registry()


class I170Author(BaseSurrealModel):
    """Target of the foreign key."""

    model_config = SurrealConfigDict(table_name="i170_authors")

    id: str | None = None
    name: str


class I170Post(BaseSurrealModel):
    """Holds a cascading record link plus a virtual graph relation."""

    model_config = SurrealConfigDict(table_name="i170_posts")

    id: str | None = None
    title: str
    subtitle: str | None = None
    author: ForeignKey("I170Author")
    tags: Relation("tagged", "I170Author")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_define_table_emits_a_typed_optional_record_link() -> None:
    """The generated DDL types the foreign key and keeps it optional."""
    sql = await I170Post.define_table()

    assert "TYPE option<record<i170_authors>> REFERENCE ON DELETE CASCADE" in sql
    assert "option<option<" not in sql


@pytest.mark.integration
@pytest.mark.asyncio
async def test_define_table_keeps_optional_scalars_optional() -> None:
    """A ``str | None`` column is still wrapped exactly once."""
    sql = await I170Post.define_table()

    assert "DEFINE FIELD subtitle ON i170_posts TYPE option<string>" in sql
    assert "DEFINE FIELD title ON i170_posts TYPE string" in sql


@pytest.mark.integration
@pytest.mark.asyncio
async def test_define_table_omits_virtual_relation_fields() -> None:
    """A graph ``Relation`` defines no column."""
    sql = await I170Post.define_table()

    assert "DEFINE FIELD tags" not in sql


@pytest.mark.integration
@pytest.mark.asyncio
async def test_generated_ddl_is_accepted_by_surrealdb() -> None:
    """SurrealDB reports the field exactly as the migration defined it."""
    await I170Post.define_table()

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    info = await client.query("INFO FOR TABLE i170_posts;")
    fields = info.first_result.result["fields"]

    assert "REFERENCE ON DELETE CASCADE" in fields["author"]
    assert "tags" not in fields


@pytest.mark.integration
@pytest.mark.asyncio
async def test_on_delete_cascade_is_enforced_by_the_server() -> None:
    """Deleting the referenced author removes the referencing post."""
    await I170Author.define_table()
    await I170Post.define_table()

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query("CREATE i170_authors:alice SET name = 'Alice';")
    await client.query("CREATE i170_posts:one SET title = 'Hello', author = i170_authors:alice;")

    await client.query("DELETE i170_authors:alice;")
    remaining = await client.query("SELECT * FROM i170_posts;")

    assert remaining.all_records == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_many_to_many_defines_no_column() -> None:
    """A ManyToMany attribute produces no DDL at all."""

    class I170Group(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="i170_groups")
        id: str | None = None
        name: str

    class I170Member(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name="i170_members")
        id: str | None = None
        name: str
        groups: ManyToMany("I170Group", through="i170_membership")

    sql = await I170Member.define_table()

    assert "DEFINE FIELD groups" not in sql

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    info = await client.query("INFO FOR TABLE i170_members;")
    fields = info.first_result.result["fields"]

    assert "groups" not in fields


@pytest.mark.integration
@pytest.mark.asyncio
async def test_schema_diff_is_empty_after_define_table() -> None:
    """A model applied to the database no longer diffs against itself.

    A foreign key used to introspect as ``any`` while the database reported
    ``record<...>``, so every ``schema_diff()`` proposed a phantom AlterField.
    """
    from src.surreal_orm.introspection import schema_diff
    from src.surreal_orm.migrations.operations import AddField, AlterField

    await I170Author.define_table()
    await I170Post.define_table()

    operations = await schema_diff(models=[I170Author, I170Post])
    # Other tables in the shared test database produce their own operations;
    # only the ones touching this model's fields matter here.
    field_ops = [
        op for op in operations if isinstance(op, (AddField, AlterField)) and op.table in ("i170_posts", "i170_authors")
    ]

    assert field_ops == []
