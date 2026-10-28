"""
Integration tests for issue #401 — text defaults and highlight tags, on the pinned server.

A ``str`` default containing ``::`` or starting with ``$`` was emitted
unquoted, and a highlight tag ending in a backslash did not render as the
text it holds. Only a server shows what the rendered statement actually does, so every test runs it
and reads the result back: the definition (``INFO FOR TABLE``), the tables that
exist (``INFO FOR DB``) and the rows returned.

Run with: pytest -m integration tests/test_issue_401_integration.py
"""

import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from src import surreal_orm
from src.surreal_orm import SurrealFunc
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.define_parser import parse_define_field
from src.surreal_orm.migrations.executor import MIGRATIONS_TABLE, MigrationExecutor
from src.surreal_orm.migrations.generator import MigrationGenerator
from src.surreal_orm.migrations.operations import (
    AddField,
    AlterField,
    CreateIndex,
    CreateTable,
    DefineAnalyzer,
    Operation,
)
from src.surreal_orm.migrations.state import FieldState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.search import SearchHighlight
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_401"

TEXTS_TABLE = "i401_texts"
NOTES = "i401_notes"
DOCS = "i401_docs"
ANALYZER = "i401_az"
WITNESS = "witness"
TABLES = [TEXTS_TABLE, NOTES, DOCS, WITNESS, MIGRATIONS_TABLE]

WITNESS_DEFAULT = "x::y; DEFINE TABLE witness; --"
TEXTS = [WITNESS_DEFAULT, "$abc::d; DEFINE TABLE witness", "fr::CA"]


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
    """Drop every table and the analyzer of the test database before and after each test."""

    async def cleanup() -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        info = await client.query("INFO FOR DB;")
        for table in set(info.first_result.result["tables"]) | set(TABLES):
            await client.query(f"REMOVE TABLE IF EXISTS {table};")
        await client.query(f"REMOVE ANALYZER IF EXISTS {ANALYZER};")

    await cleanup()
    yield
    await cleanup()


@pytest.fixture
def migrations_dir() -> Iterator[Path]:
    """A throwaway migrations directory."""
    path = Path(tempfile.mkdtemp())
    yield path
    shutil.rmtree(path)


async def _query(sql: str) -> list[object]:
    """Run SQL, fail the test on any statement-level error, return each statement's result."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value == "ERR"]
    assert errors == [], f"{errors}\nSQL: {sql}"
    return [r.result for r in response.results]


async def _tables() -> set[str]:
    return set((await _query("INFO FOR DB;"))[0]["tables"])  # type: ignore[index]


async def _field(table: str, name: str) -> FieldState:
    fields = (await _query(f"INFO FOR TABLE {table};"))[0]["fields"]  # type: ignore[index]
    return parse_define_field(fields[name])


def _operations_for(operations: list[Operation], *tables: str) -> list[Operation]:
    """Keep the operations touching the tables under test."""
    return [op for op in operations if (getattr(op, "table", None) or getattr(op, "name", None)) in tables]


# ------------------------------------------------------------------ defaults


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("text", TEXTS)
async def test_text_default_is_stored_as_text(text: str) -> None:
    await _query(CreateTable(name=TEXTS_TABLE, schema_mode="SCHEMALESS").forwards())
    await _query(AddField(table=TEXTS_TABLE, name="f", field_type="string", default=text).forwards())

    assert WITNESS not in await _tables()
    assert (await _field(TEXTS_TABLE, "f")).default == text
    created = await _query(f"CREATE {TEXTS_TABLE}:one RETURN f;")
    assert created[0][0]["f"] == text  # type: ignore[index]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("text", TEXTS)
async def test_rollback_replays_the_default_read_from_the_database(text: str) -> None:
    await _query(CreateTable(name=TEXTS_TABLE, schema_mode="SCHEMALESS").forwards())
    await _query(AddField(table=TEXTS_TABLE, name="f", field_type="string", default=text).forwards())

    current = await _field(TEXTS_TABLE, "f")
    target = FieldState(name="f", field_type="string", nullable=False, default="other")
    op = AlterField.from_field_states(TEXTS_TABLE, current, target)

    await _query(op.forwards())
    assert (await _field(TEXTS_TABLE, "f")).default == "other"
    await _query(op.backwards())

    assert WITNESS not in await _tables()
    assert (await _field(TEXTS_TABLE, "f")).default == text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expression_default_is_still_evaluated() -> None:
    await _query(CreateTable(name=TEXTS_TABLE, schema_mode="SCHEMALESS").forwards())
    await _query(AddField(table=TEXTS_TABLE, name="at", field_type="datetime", default=SurrealFunc("time::now()")).forwards())

    current = await _field(TEXTS_TABLE, "at")
    assert current.default == SurrealFunc("time::now()")
    op = AlterField.from_field_states(TEXTS_TABLE, current, FieldState(name="at", field_type="datetime", nullable=False))
    await _query(op.forwards())
    await _query(op.backwards())

    assert (await _field(TEXTS_TABLE, "at")).default == SurrealFunc("time::now()")
    created = await _query(f"CREATE {TEXTS_TABLE}:one RETURN type::is_datetime(at) AS ok;")
    assert created[0][0]["ok"] is True  # type: ignore[index]


class I401Note(BaseSurrealModel):
    """A text default that looks like an expression, and a legacy expression default."""

    model_config = SurrealConfigDict(table_name=NOTES, schema_mode="SCHEMAFULL")

    id: str | None = None
    label: str = WITNESS_DEFAULT
    stamp: str = "time::now()"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Passing a server-side expression:DeprecationWarning")
async def test_model_defaults_migrate_and_converge(migrations_dir: Path) -> None:
    operations = _operations_for(await schema_diff(models=[I401Note]), NOTES)
    assert operations, "precondition: the table does not exist yet"

    MigrationGenerator(migrations_dir).generate(name="notes", operations=operations)
    await MigrationExecutor(migrations_dir).migrate()

    assert WITNESS not in await _tables()
    assert (await _field(NOTES, "label")).default == WITNESS_DEFAULT
    assert (await _field(NOTES, "stamp")).default == SurrealFunc("time::now()")
    assert _operations_for(await schema_diff(models=[I401Note]), NOTES) == []


# ------------------------------------------------------------------ SearchHighlight


class I401Doc(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=DOCS)

    id: str | None = None
    title: str


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("open_tag", "close_tag"),
    [("a\\", "</b>"), ("a\\'; DEFINE TABLE witness; --", "\\")],
)
async def test_highlight_tags_are_returned_as_written(open_tag: str, close_tag: str) -> None:
    await _query(DefineAnalyzer(name=ANALYZER, tokenizers=["blank"], filters=["lowercase"]).forwards())
    await _query(CreateTable(name=DOCS, schema_mode="SCHEMALESS").forwards())
    await _query(
        CreateIndex(
            table=DOCS, name="i401_ft", fields=["title"], search_analyzer=ANALYZER, bm25=True, highlights=True
        ).forwards()
    )
    await _query(f"CREATE {DOCS}:one SET title = 'quantum physics';")

    query = I401Doc.objects().search(title="quantum").annotate(snippet=SearchHighlight(open_tag, close_tag, 0))
    rows = await query.exec()

    assert WITNESS not in await _tables()
    assert len(rows) == 1
    assert getattr(rows[0], "snippet", None) == f"{open_tag}quantum{close_tag} physics"
