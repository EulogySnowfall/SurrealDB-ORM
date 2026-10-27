"""
Integration tests for issue #195 — a field's PERMISSIONS survive the ORM altering it.

The ORM does not model field-level ``PERMISSIONS``. It used to redefine a field
with ``DEFINE FIELD OVERWRITE``, which replaces the whole definition, so a
restriction set outside the ORM fell back to the field default
(``PERMISSIONS FULL``) on the next ``AlterField`` — forwards and rollback — and on
every ``define_table()``. ``ALTER FIELD`` changes only the clauses it names.

Only a server shows the consequence, so every test reads ``INFO FOR TABLE`` back.

Run with: pytest -m integration tests/test_issue_195_integration.py
"""

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from src import surreal_orm
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.executor import MIGRATIONS_TABLE, MigrationExecutor, MigrationStatementError
from src.surreal_orm.migrations.generator import MigrationGenerator
from src.surreal_orm.migrations.operations import AddField, AlterField, Operation
from src.surreal_orm.migrations.state import FieldState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, clear_model_registry
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_195"

DOCS = "i195_docs"
NOTES = "i195_notes"
AUTHORS = "i195_authors"
EDITORS = "i195_editors"
TAGGED = "i195_tagged"
TABLES = [DOCS, NOTES, AUTHORS, EDITORS, TAGGED, MIGRATIONS_TABLE]

#: How SurrealDB reports ``PERMISSIONS FOR select NONE`` on a field: the other
#: actions keep the field default, FULL. Losing the restriction reads ``PERMISSIONS FULL``.
RESTRICTED = "PERMISSIONS FOR select NONE, FOR create, update FULL"


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
    """Drop the test tables and the migration record before and after each test."""

    async def cleanup() -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        for table in TABLES:
            await client.query(f"REMOVE TABLE IF EXISTS {table};")

    await cleanup()
    yield
    await cleanup()


@pytest.fixture
def migrations_dir() -> Iterator[Path]:
    """A throwaway migrations directory."""
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path)


async def _query(sql: str) -> None:
    """Run SQL and fail the test on any statement-level error."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value == "ERR"]
    assert errors == [], f"{errors}\nSQL: {sql}"


async def _fields(table: str) -> dict[str, str]:
    """The field definitions as SurrealDB reports them."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    info = await client.query(f"INFO FOR TABLE {table};")
    return dict(info.first_result.result["fields"])


def _permissions(definition: str) -> str:
    """The PERMISSIONS clause of a reported field definition."""
    return definition[definition.index("PERMISSIONS") :]


async def _restrict(table: str, field: str, permission: str = "FOR select NONE") -> str:
    """Restrict a field by hand, outside the ORM, and return its PERMISSIONS clause."""
    await _query(f"ALTER FIELD {field} ON {table} PERMISSIONS {permission};")
    permissions = _permissions((await _fields(table))[field])
    assert permissions != "PERMISSIONS FULL", "precondition: the field should be restricted"
    return permissions


async def _write_errors(sql: str) -> list[str]:
    """The statement-level errors of a write, for asserting that it is accepted."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    return [str(r.result) for r in response.results if r.status.value == "ERR"]


async def _restricted_docs_table() -> None:
    """A field whose permission was set by hand, outside the ORM."""
    await _query(f"DEFINE TABLE {DOCS} SCHEMAFULL;DEFINE FIELD secret ON {DOCS} TYPE string PERMISSIONS FOR select NONE;")


def _operations_for(operations: list[Operation], *tables: str) -> list[Operation]:
    """Keep the operations touching the tables under test; the database is shared."""
    return [op for op in operations if (getattr(op, "table", None) or getattr(op, "name", None)) in tables]


# --------------------------------------------------------------------------- AlterField


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_applied_alter_field_keeps_the_field_permissions(migrations_dir: Path) -> None:
    """Criterion 1: forwards() changed the type and assertion, and nothing else."""
    await _restricted_docs_table()

    op = AlterField.from_field_states(
        DOCS,
        FieldState(name="secret", field_type="string", nullable=False),
        FieldState(name="secret", field_type="string", nullable=True, assertion="$value != ''"),
    )
    MigrationGenerator(migrations_dir).generate(name="alter_secret", operations=[op])
    await MigrationExecutor(migrations_dir).migrate()

    definition = (await _fields(DOCS))["secret"]

    assert "TYPE none | string" in definition, "the alteration itself did not happen"
    assert "ASSERT $value != ''" in definition, definition
    assert RESTRICTED in definition, f"the field permission was reset: {definition}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rolled_back_alter_field_keeps_the_field_permissions(migrations_dir: Path) -> None:
    """Criterion 2: the rollback restores the previous definition, permission included."""
    await _restricted_docs_table()
    original = (await _fields(DOCS))["secret"]

    op = AlterField.from_field_states(
        DOCS,
        FieldState(name="secret", field_type="string", nullable=False),
        FieldState(name="secret", field_type="int", nullable=False),
    )
    MigrationGenerator(migrations_dir).generate(name="retype_secret", operations=[op])
    executor = MigrationExecutor(migrations_dir)
    await executor.migrate()
    assert "TYPE int" in (await _fields(DOCS))["secret"]

    await executor.rollback("")

    assert (await _fields(DOCS))["secret"] == original


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_alter_field_on_a_missing_field_fails_the_migration(migrations_dir: Path) -> None:
    """Criterion 9: an error naming the field, never a silent no-op and never a creation.

    ``ALTER FIELD`` requires the field. A migration altering a field that is not
    there is out of step with the database; ``AddField`` is the operation that
    creates one.
    """
    await _query(f"DEFINE TABLE {DOCS} SCHEMAFULL;")

    op = AlterField(table=DOCS, name="ghost", field_type="int", previous_type="string")
    MigrationGenerator(migrations_dir).generate(name="alter_ghost", operations=[op])

    with pytest.raises(MigrationStatementError, match="The field 'ghost' does not exist"):
        await MigrationExecutor(migrations_dir).migrate()

    assert "ghost" not in await _fields(DOCS)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_modelled_clauses_are_removed_and_restored_around_the_permission() -> None:
    """Criterion 4: removing a clause from the model still removes it; rollback brings it back.

    Every modelled clause at once, over three fields since FLEXIBLE needs an
    object and REFERENCE a record. The permission must ride through both
    directions, and the rollback must give back the exact original definitions.
    """
    await _query(
        f"DEFINE TABLE {AUTHORS} SCHEMAFULL;"
        f"DEFINE TABLE {DOCS} SCHEMAFULL;"
        f"DEFINE FIELD meta ON {DOCS} TYPE object FLEXIBLE COMMENT 'kept' PERMISSIONS FOR select NONE;"
        f"DEFINE FIELD n ON {DOCS} TYPE int DEFAULT 3 READONLY ASSERT $value > 0 PERMISSIONS FOR select NONE;"
        f"DEFINE FIELD author ON {DOCS} TYPE option<record<{AUTHORS}>> VALUE $value "
        "REFERENCE ON DELETE CASCADE PERMISSIONS FOR select NONE;"
    )
    before = await _fields(DOCS)

    meta = AlterField.from_field_states(
        DOCS,
        FieldState(name="meta", field_type="object", nullable=False, flexible=True, comment="kept"),
        FieldState(name="meta", field_type="object", nullable=True),
    )
    n = AlterField.from_field_states(
        DOCS,
        FieldState(name="n", field_type="int", nullable=False, default=3, readonly=True, assertion="$value > 0"),
        FieldState(name="n", field_type="int", nullable=False),
    )
    author = AlterField.from_field_states(
        DOCS,
        FieldState(
            name="author",
            field_type=f"record<{AUTHORS}>",
            nullable=True,
            value="$value",
            reference=True,
            on_delete="CASCADE",
        ),
        FieldState(name="author", field_type=f"record<{AUTHORS}>", nullable=True),
    )

    await _query(meta.forwards() + n.forwards() + author.forwards())
    stripped = await _fields(DOCS)

    assert stripped == {
        "meta": f"DEFINE FIELD meta ON {DOCS} TYPE none | object {RESTRICTED}",
        "n": f"DEFINE FIELD n ON {DOCS} TYPE int {RESTRICTED}",
        "author": f"DEFINE FIELD author ON {DOCS} TYPE none | record<{AUTHORS}> {RESTRICTED}",
    }

    await _query(author.backwards() + n.backwards() + meta.backwards())

    assert await _fields(DOCS) == before


# --------------------------------------------------------------------- schema_diff / makemigrations


class I195Note(BaseSurrealModel):
    """The schema as first migrated."""

    model_config = SurrealConfigDict(table_name=NOTES)

    id: str | None = None
    title: str
    type: str = "public"
    secret: str


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permission",
    [
        "FOR select NONE",
        # The usual shape of a field permission. SurrealDB reports the condition
        # with field names unescaped, and `type` is also a clause keyword.
        "FOR select WHERE type = 'public'",
        "FOR select WHERE comment = 'x' OR default = 1 OR readonly = true",
    ],
)
async def test_an_unrelated_model_change_leaves_the_permission_alone(migrations_dir: Path, permission: str) -> None:
    """Criterion 3: a diff that touches other fields does not reset this one, and converges."""
    await I195Note.define_table()
    restricted = await _restrict(NOTES, "secret", permission)

    clear_model_registry()

    class I195NoteChanged(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name=NOTES)
        id: str | None = None
        title: str | None = None  # altered
        type: str = "public"
        secret: str
        views: int = 0  # added

    operations = _operations_for(await schema_diff(models=[I195NoteChanged]), NOTES)
    assert {(type(op).__name__, getattr(op, "name", None)) for op in operations} == {
        ("AlterField", "title"),
        ("AddField", "views"),
    }

    MigrationGenerator(migrations_dir).generate(name="unrelated", operations=operations)
    await MigrationExecutor(migrations_dir).migrate()

    assert _permissions((await _fields(NOTES))["secret"]) == restricted
    assert _operations_for(await schema_diff(models=[I195NoteChanged]), NOTES) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_generated_alteration_of_the_field_keeps_its_permission(migrations_dir: Path) -> None:
    """Criterion 3, on the field itself: the generated AlterField must not reset it either way."""
    await I195Note.define_table()
    restricted = await _restrict(NOTES, "secret")
    original = (await _fields(NOTES))["secret"]

    clear_model_registry()

    class I195NoteOptional(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name=NOTES)
        id: str | None = None
        title: str
        type: str = "public"
        secret: str | None = None  # altered

    operations = _operations_for(await schema_diff(models=[I195NoteOptional]), NOTES)
    assert [(type(op).__name__, getattr(op, "name", None)) for op in operations] == [("AlterField", "secret")]

    MigrationGenerator(migrations_dir).generate(name="optional_secret", operations=operations)
    executor = MigrationExecutor(migrations_dir)
    await executor.migrate()

    altered = (await _fields(NOTES))["secret"]
    assert "TYPE none | string" in altered, altered
    assert _permissions(altered) == restricted, f"the generated migration reset the permission: {altered}"
    assert _operations_for(await schema_diff(models=[I195NoteOptional]), NOTES) == []

    await executor.rollback("")

    assert (await _fields(NOTES))["secret"] == original


# --------------------------------------------------------------------------- define_table()


class I195Doc(BaseSurrealModel):
    """``define_table()`` applies this over a field restricted by hand."""

    model_config = SurrealConfigDict(table_name=DOCS)

    id: str | None = None
    title: str
    secret: str | None = None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_define_table_again_keeps_a_hand_defined_field_permission() -> None:
    """Criterion 7: re-applying the model's schema left the field world-readable."""
    await I195Doc.define_table()
    await _restrict(DOCS, "secret")
    restricted = (await _fields(DOCS))["secret"]

    await I195Doc.define_table()

    assert (await _fields(DOCS))["secret"] == restricted


@pytest.mark.integration
@pytest.mark.asyncio
async def test_define_table_still_applies_the_model_over_an_existing_field() -> None:
    """The ALTER half: the modelled clauses still follow the model, permission aside."""
    await _query(
        f"DEFINE TABLE {DOCS} SCHEMAFULL;"
        f"DEFINE FIELD secret ON {DOCS} TYPE int DEFAULT 3 ASSERT $value > 0 PERMISSIONS FOR select NONE;"
    )

    await I195Doc.define_table()
    secret = (await _fields(DOCS))["secret"]

    assert secret == f"DEFINE FIELD secret ON {DOCS} TYPE none | string {RESTRICTED}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_define_table_on_an_empty_database_and_twice() -> None:
    """Criterion 8: the IF NOT EXISTS half creates; a second call is not an error."""
    await I195Doc.define_table()
    first = await _fields(DOCS)

    assert first == {
        "title": f"DEFINE FIELD title ON {DOCS} TYPE string PERMISSIONS FULL",
        "secret": f"DEFINE FIELD secret ON {DOCS} TYPE none | string PERMISSIONS FULL",
    }

    await I195Doc.define_table()

    assert await _fields(DOCS) == first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_add_field_still_refuses_an_existing_field() -> None:
    """AddField keeps the plain DEFINE: adding a field that is there is a real conflict."""
    await _restricted_docs_table()
    client = await surreal_orm.SurrealDBConnectionManager.get_client()

    response = await client.query(AddField(table=DOCS, name="secret", field_type="int").forwards())

    assert [r.status.value for r in response.results] == ["ERR"]
    assert RESTRICTED in (await _fields(DOCS))["secret"]


# ------------------------------------------------------------- array element sub-fields


def _retype_tags(previous: str, target: str) -> AlterField:
    """The AlterField makemigrations generates for a changed list element type."""
    return AlterField.from_field_states(
        TAGGED,
        FieldState(name="tags", field_type=previous, nullable=False),
        FieldState(name="tags", field_type=target, nullable=False),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_array_retype_keeps_the_column_writable_both_ways(migrations_dir: Path) -> None:
    """SurrealDB derives ``tags.*``; ALTER FIELD on ``tags`` alone left it at the old element type.

    The column then refused every non-empty value: ``Couldn't coerce value for
    field `tags.*```. DEFINE FIELD OVERWRITE re-derived it, so this worked
    while AlterField emitted that.
    """
    await _query(f"DEFINE TABLE {TAGGED} SCHEMAFULL; DEFINE FIELD tags ON {TAGGED} TYPE array<string>;")

    MigrationGenerator(migrations_dir).generate(name="retype_tags", operations=[_retype_tags("array<string>", "array<int>")])
    executor = MigrationExecutor(migrations_dir)
    await executor.migrate()

    assert (await _fields(TAGGED))["tags.*"] == f"DEFINE FIELD tags.* ON {TAGGED} TYPE int PERMISSIONS FULL"
    assert await _write_errors(f"CREATE {TAGGED}:forwards SET tags = [1, 2];") == []

    await _query(f"DELETE {TAGGED};")
    await executor.rollback("")

    assert (await _fields(TAGGED))["tags.*"] == f"DEFINE FIELD tags.* ON {TAGGED} TYPE string PERMISSIONS FULL"
    assert await _write_errors(f"CREATE {TAGGED}:backwards SET tags = ['a', 'b'];") == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rolling_back_an_array_retype_applied_by_overwrite() -> None:
    """A database migrated forward by 0.34.x has ``tags.*`` in the new element type already."""
    await _query(f"DEFINE TABLE {TAGGED} SCHEMAFULL; DEFINE FIELD tags ON {TAGGED} TYPE array<int>;")
    assert (await _fields(TAGGED))["tags.*"] == f"DEFINE FIELD tags.* ON {TAGGED} TYPE int PERMISSIONS FULL"

    await _query(_retype_tags("array<string>", "array<int>").backwards())

    assert (await _fields(TAGGED))["tags.*"] == f"DEFINE FIELD tags.* ON {TAGGED} TYPE string PERMISSIONS FULL"
    assert await _write_errors(f"CREATE {TAGGED}:1 SET tags = ['a', 'b'];") == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_nested_array_retype_aligns_every_level() -> None:
    """``array<array<…>>`` derives ``n.*`` and ``n.*.*``."""
    await _query(f"DEFINE TABLE {TAGGED} SCHEMAFULL; DEFINE FIELD tags ON {TAGGED} TYPE array<array<string>>;")

    await _query(_retype_tags("array<array<string>>", "array<array<int>>").forwards())
    fields = await _fields(TAGGED)

    assert fields["tags.*"] == f"DEFINE FIELD tags.* ON {TAGGED} TYPE array<int> PERMISSIONS FULL"
    assert fields["tags.*.*"] == f"DEFINE FIELD tags.*.* ON {TAGGED} TYPE int PERMISSIONS FULL"
    assert await _write_errors(f"CREATE {TAGGED}:1 SET tags = [[1, 2], [3]];") == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_changed_reference_target_reaches_the_element() -> None:
    """A ReferencesField is ``option<array<record<T>>>``: ``o.*`` kept ``record<old>``."""
    await _query(
        f"DEFINE TABLE {AUTHORS} SCHEMAFULL; DEFINE TABLE {EDITORS} SCHEMAFULL; DEFINE TABLE {TAGGED} SCHEMAFULL;"
        f"DEFINE FIELD o ON {TAGGED} TYPE option<array<record<{AUTHORS}>>> REFERENCE ON DELETE IGNORE;"
    )
    op = AlterField.from_field_states(
        TAGGED,
        FieldState(name="o", field_type=f"array<record<{AUTHORS}>>", nullable=True, reference=True, on_delete="IGNORE"),
        FieldState(name="o", field_type=f"array<record<{EDITORS}>>", nullable=True, reference=True, on_delete="IGNORE"),
    )

    await _query(op.forwards())

    assert (await _fields(TAGGED))["o.*"] == f"DEFINE FIELD o.* ON {TAGGED} TYPE record<{EDITORS}> PERMISSIONS FULL"
    assert await _write_errors(f"CREATE {TAGGED}:1 SET o = [{EDITORS}:1];") == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_define_table_again_after_the_element_type_changed() -> None:
    """The define_table() pair goes through the same renderer, so ``tags.*`` follows the model."""

    class I195TaggedStr(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name=TAGGED)
        id: str | None = None
        tags: list[str] = []

    await I195TaggedStr.define_table()
    assert (await _fields(TAGGED))["tags.*"] == f"DEFINE FIELD tags.* ON {TAGGED} TYPE string PERMISSIONS FULL"

    clear_model_registry()

    class I195TaggedInt(BaseSurrealModel):
        model_config = SurrealConfigDict(table_name=TAGGED)
        id: str | None = None
        tags: list[int] = []

    await I195TaggedInt.define_table()

    assert (await _fields(TAGGED))["tags.*"] == f"DEFINE FIELD tags.* ON {TAGGED} TYPE int PERMISSIONS FULL"
    saved = I195TaggedInt(tags=[1, 2])
    await saved.save()
    assert (await I195TaggedInt.objects().get(saved.get_id())).tags == [1, 2]
