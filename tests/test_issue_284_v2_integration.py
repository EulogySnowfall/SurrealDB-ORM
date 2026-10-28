"""
Integration tests for #284 on the v2 line, against a live SurrealDB 2.x server.

Migration DDL quoted a COMMENT or a string DEFAULT by doubling the apostrophe,
which SurrealDB 2.x does not read as an escaped quote, and left backslashes
unescaped. The unit tests pin the rendered literal; these run it on the server
and read it back the way the ORM does, through ``INFO FOR ...``.

Run with: pytest -m integration tests/test_issue_284_v2_integration.py
"""

import re
from typing import Any

import pytest

from src import surreal_orm
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector
from src.surreal_orm.migrations.define_parser import _unquote_surql_string
from src.surreal_orm.migrations.introspector import introspect_models
from src.surreal_orm.migrations.operations import (
    CreateIndex,
    DefineAccess,
    DefineEvent,
    Operation,
    _render_define_field,
    _render_define_table,
)
from src.surreal_orm.migrations.state import SchemaState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_284_v2"
NOTE = "i284_note"
DOC = "i284_doc"
ACCESS = "i284_access"

#: Quotes, backslashes, and text that reads like the rest of a statement.
TEXTS = [
    "it's",
    'say "hi"',
    'it\'s "x"',
    "C:\\temp",
    "a\\' ; DEFINE TABLE i284_other; -- b",
    "end\\",
    "x::y; DEFINE TABLE i284_other; -- z",
    "$value",
]


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


async def _info_for_db() -> dict[str, Any]:
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    info = await client.query("INFO FOR DB;")
    result = info.first_result.result
    assert isinstance(result, dict)
    return result


@pytest.fixture(autouse=True)
async def clean_database():
    """Empty this file's database before and after each test."""

    async def cleanup() -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        info = await _info_for_db()
        statements = [f"REMOVE TABLE IF EXISTS {name};" for name in info.get("tables", {})]
        statements += [f"REMOVE ACCESS IF EXISTS {name} ON DATABASE;" for name in info.get("accesses", {})]
        if statements:
            await client.query(" ".join(statements))

    await cleanup()
    yield
    await cleanup()


class I284Note(BaseSurrealModel):
    """A table comment and a string default, both with an apostrophe."""

    model_config = SurrealConfigDict(table_name=NOTE, schema_mode="SCHEMAFULL", comment="Notes, it's said")

    id: str | None = None
    label: str = 'it\'s "x"'
    path: str = "C:\\temp"


async def _run(sql: str) -> None:
    """Execute *sql* and fail on any statement the server rejected.

    ``client.query()`` only raises on an RPC failure; a rejected statement
    comes back as ``status: ERR`` inside a successful response.
    """
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value != "OK"]
    assert errors == [], f"{errors}\nSQL: {sql}"


def _on_note(operations: list[Operation]) -> list[Operation]:
    return [op for op in operations if getattr(op, "table", getattr(op, "name", None)) == NOTE]


def _reported_comment(definition: str) -> str:
    """The text of the COMMENT clause of a definition, as the server reports it."""
    match = re.search(r"""\bCOMMENT\s+("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""", definition)
    assert match, definition
    return _unquote_surql_string(match.group(1))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_model_with_apostrophes_migrates_and_converges() -> None:
    """The model migrates, and a schema diff right after emits nothing for it."""
    operations = _on_note(SchemaState().diff(introspect_models([I284Note])))
    assert operations

    for op in operations:
        await _run(op.forwards())

    table = (await DatabaseIntrospector().introspect()).tables[NOTE]
    assert table.comment == "Notes, it's said"
    assert table.fields["label"].default == 'it\'s "x"'
    assert table.fields["path"].default == "C:\\temp"

    assert _on_note(await schema_diff(models=[I284Note])) == []


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("text", TEXTS)
async def test_comment_text_round_trips(text: str) -> None:
    """Every rendered COMMENT and DEFAULT reads back as the text it was given.

    The definitions are executed as rendered; the database then holds exactly
    the two tables and the access defined here, nothing else.
    """
    await _run(_render_define_table(DOC, overwrite=False, schema_mode="SCHEMAFULL", comment=text))
    await _run(_render_define_field(DOC, "body", overwrite=False, field_type="string", default=text, comment=text))
    await _run(CreateIndex(table=DOC, name="i284_idx", fields=["body"], comment=text).forwards())
    await _run(DefineEvent(name="i284_evt", table=DOC, when="true", then="CREATE i284_log", comment=text).forwards())
    await _run(
        DefineAccess(
            name=ACCESS,
            table=DOC,
            signup_fields={"body": "$body"},
            signin_where="body = $body",
            comment=text,
        ).forwards()
    )

    state = (await DatabaseIntrospector().introspect()).tables[DOC]
    assert state.comment == text
    assert state.fields["body"].comment == text
    assert state.fields["body"].default == text

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    table_info = (await client.query(f"INFO FOR TABLE {DOC};")).first_result.result
    assert _reported_comment(table_info["indexes"]["i284_idx"]) == text
    assert _reported_comment(table_info["events"]["i284_evt"]) == text
    assert set(table_info["fields"]) == {"body"}

    db_info = await _info_for_db()
    assert _reported_comment(db_info["accesses"][ACCESS]) == text
    assert set(db_info["tables"]) == {DOC}
    assert set(db_info["accesses"]) == {ACCESS}
    for kind in ("analyzers", "functions", "params", "users", "models"):
        assert db_info.get(kind, {}) == {}, kind


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("text", TEXTS)
async def test_default_text_replays_unchanged(text: str) -> None:
    """A string DEFAULT read back from the server re-renders as the same text.

    ``schema_diff`` and ``AlterField`` (forwards, and backwards through
    ``previous_default``) render what the parser read from ``INFO FOR TABLE``.
    A text containing ``::`` or starting with ``$`` must stay a text there.
    """
    await _run(_render_define_table(DOC, overwrite=False, schema_mode="SCHEMAFULL"))
    await _run(_render_define_field(DOC, "body", overwrite=False, field_type="string", default=text))

    read_back = (await DatabaseIntrospector().introspect()).tables[DOC].fields["body"].default
    assert read_back == text

    await _run(_render_define_field(DOC, "body", overwrite=True, field_type="string", default=read_back))

    assert (await DatabaseIntrospector().introspect()).tables[DOC].fields["body"].default == text
    db_info = await _info_for_db()
    assert set(db_info["tables"]) == {DOC}
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    table_info = (await client.query(f"INFO FOR TABLE {DOC};")).first_result.result
    assert set(table_info["fields"]) == {"body"}
