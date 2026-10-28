"""
Integration tests for issue #283 — string literals in migration DDL, on the pinned server.

A ``COMMENT`` or a string ``DEFAULT`` holding an apostrophe was rendered as
``'it''s'``, which SurrealDB rejects, and a backslash was not escaped at all.
The server reports a string holding an apostrophe in double quotes, which the
parser read back with its quotes. Only a server shows both halves, so every test
executes the rendered DDL and reads the definition back.

Run with: pytest -m integration tests/test_issue_283_integration.py
"""

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from src import surreal_orm
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector
from src.surreal_orm.migrations.define_parser import parse_define_field, parse_define_table
from src.surreal_orm.migrations.executor import MIGRATIONS_TABLE, MigrationExecutor
from src.surreal_orm.migrations.generator import MigrationGenerator
from src.surreal_orm.migrations.operations import (
    AddField,
    AlterField,
    CreateIndex,
    CreateTable,
    DefineAccess,
    DefineApi,
    DefineBearerAccess,
    DefineEvent,
    Operation,
)
from src.surreal_orm.migrations.state import FieldState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.utils import parse_surql_string
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_283"

PEOPLE = "i283_people"
NOTES = "i283_notes"
TEXTS_TABLE = "i283_texts"
TABLES = [PEOPLE, NOTES, TEXTS_TABLE, MIGRATIONS_TABLE]

TEXTS = ["it's", 'say "hi"', 'it\'s "x"', "C:\\temp", "C:\\"]


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


async def _info(what: str) -> dict[str, dict[str, str]]:
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(f"INFO FOR {what};")
    return dict(response.first_result.result)


def _operations_for(operations: list[Operation], *tables: str) -> list[Operation]:
    """Keep the operations touching the tables under test."""
    return [op for op in operations if (getattr(op, "table", None) or getattr(op, "name", None)) in tables]


# ------------------------------------------------------------------ every renderer


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("text", TEXTS)
async def test_every_rendered_literal_is_accepted_and_read_back(text: str) -> None:
    """Field and table COMMENT, a string DEFAULT, index and event COMMENT."""
    await _query(CreateTable(name=TEXTS_TABLE, schema_mode="SCHEMALESS", comment=text).forwards())
    await _query(AddField(table=TEXTS_TABLE, name="f", field_type="string", default=text, comment=text).forwards())
    await _query(CreateIndex(table=TEXTS_TABLE, name="i283_idx", fields=["f"], comment=text).forwards())
    await _query(DefineEvent(name="i283_ev", table=TEXTS_TABLE, when="false", then="RETURN 1", comment=text).forwards())

    table = parse_define_table((await _info("DB"))["tables"][TEXTS_TABLE])
    assert table["comment"] == text

    info = await _info(f"TABLE {TEXTS_TABLE}")
    field = parse_define_field(info["fields"]["f"])
    assert (field.comment, field.default) == (text, text)
    for kind, name in (("indexes", "i283_idx"), ("events", "i283_ev")):
        definition = info[kind][name]
        assert parse_surql_string(definition[definition.rindex(" COMMENT ") + 9 :]) == text, definition


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("text", TEXTS)
async def test_database_level_comments_are_accepted_and_read_back(text: str) -> None:
    """Record and bearer access, and API, COMMENT clauses."""
    operations: list[Operation] = [
        DefineAccess(
            name="i283_acc",
            table=TEXTS_TABLE,
            signup_fields={"email": "$email"},
            signin_where="email = $email",
            comment=text,
        ),
        DefineBearerAccess(name="i283_bearer", comment=text),
        DefineApi(name="/i283", method="GET", handler="RETURN 1", comment=text),
    ]
    try:
        for op in operations:
            await _query(op.forwards())
        info = await _info("DB")
        definitions = [info["accesses"]["i283_acc"], info["accesses"]["i283_bearer"], *info["apis"].values()]
        for definition in definitions:
            assert parse_surql_string(definition[definition.rindex(" COMMENT ") + 9 :]) == text, definition
    finally:
        for op in operations:
            await _query(op.backwards())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_comment_text_round_trips() -> None:
    text = "a\\' ; DEFINE TABLE i283_other; -- b ' c \\ d"
    before = set((await _info("DB"))["tables"])

    await _query(CreateTable(name=TEXTS_TABLE, schema_mode="SCHEMALESS", comment=text).forwards())
    await _query(AddField(table=TEXTS_TABLE, name="f", field_type="string", default=text, comment=text).forwards())

    tables = (await _info("DB"))["tables"]
    assert set(tables) - before == {TEXTS_TABLE}
    assert parse_define_table(tables[TEXTS_TABLE])["comment"] == text
    fields = (await _info(f"TABLE {TEXTS_TABLE}"))["fields"]
    assert set(fields) == {"f"}
    field = parse_define_field(fields["f"])
    assert (field.comment, field.default) == (text, text)


# ------------------------------------------------------------------ makemigrations / migrate


class I283Person(BaseSurrealModel):
    """A table comment and a string default the server reports in double quotes."""

    model_config = SurrealConfigDict(table_name=PEOPLE, schema_mode="SCHEMAFULL", comment="User's table")

    id: str | None = None
    nickname: str = "O'Brien C:\\temp"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_model_with_apostrophes_migrates_and_converges(migrations_dir: Path) -> None:
    operations = _operations_for(await schema_diff(models=[I283Person]), PEOPLE)
    assert operations, "precondition: the table does not exist yet"

    MigrationGenerator(migrations_dir).generate(name="people", operations=operations)
    await MigrationExecutor(migrations_dir).migrate()

    assert _operations_for(await schema_diff(models=[I283Person]), PEOPLE) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_alter_field_from_an_introspected_comment_is_accepted() -> None:
    await _query(f"DEFINE TABLE {NOTES} SCHEMAFULL; DEFINE FIELD title ON {NOTES} TYPE string COMMENT 'it\\'s \"x\"';")
    state = await DatabaseIntrospector().introspect()
    introspected = state.tables[NOTES].fields["title"]
    assert introspected.comment == 'it\'s "x"'

    target = FieldState(name="title", field_type="string", comment="Owner's title")
    op = AlterField.from_field_states(NOTES, introspected, target)

    await _query(op.forwards())
    assert parse_define_field((await _info(f"TABLE {NOTES}"))["fields"]["title"]).comment == "Owner's title"

    await _query(op.backwards())
    assert parse_define_field((await _info(f"TABLE {NOTES}"))["fields"]["title"]).comment == 'it\'s "x"'
