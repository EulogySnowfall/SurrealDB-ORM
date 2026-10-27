"""
Integration tests for #259 on the v2 line, against a live SurrealDB 2.x server.

``AlterField`` redefines a field with ``DEFINE FIELD OVERWRITE``, which replaces
the whole definition. Field permissions were not carried, so a permission set
outside the ORM was reset to ``PERMISSIONS FULL`` by the alter, and again by its
rollback. The unit tests pin the emitted SQL; only the server shows that the
clause it reports back is the one it had before.

Each check compares the PERMISSIONS clause ``INFO FOR TABLE`` reports, byte for
byte, rather than looking for part of it.

Run with: pytest -m integration tests/test_issue_259_v2_integration.py
"""

from typing import Any

import pytest

from src import surreal_orm
from src.surreal_orm.introspection import schema_diff
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector
from src.surreal_orm.migrations.introspector import introspect_models
from src.surreal_orm.migrations.operations import AddField, AlterField, CreateTable, Operation
from src.surreal_orm.migrations.state import FieldState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_259_v2"
ALT = "i259_alt"
DOC = "i259_doc"

#: The attributes a model can describe on this line — everything but permissions.
_MODELLED = ("name", "field_type", "nullable", "default", "assertion", "encrypted", "flexible", "readonly", "value", "comment")


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
        await client.query(f"REMOVE TABLE IF EXISTS {ALT}; REMOVE TABLE IF EXISTS {DOC};")

    await cleanup()
    yield
    await cleanup()


class I259Doc(BaseSurrealModel):
    """The model side of the diff test. ``extra`` is the unrelated change."""

    model_config = SurrealConfigDict(table_name=DOC)

    id: str | None = None
    title: str
    n: int
    extra: str | None = None


async def _run(sql: str) -> None:
    """Execute *sql* and fail on any statement the server rejected.

    ``client.query()`` only raises on an RPC failure; a rejected statement
    comes back as ``status: ERR`` inside a successful response.
    """
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value != "OK"]
    assert errors == [], f"{errors}\nSQL: {sql}"


async def _definition(table: str, name: str) -> str:
    """The field definition exactly as ``INFO FOR TABLE`` reports it."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    info = await client.query(f"INFO FOR TABLE {table};")
    definition = info.first_result.result["fields"][name]
    assert isinstance(definition, str)
    return definition


def _clause(definition: str) -> str:
    """From ``PERMISSIONS`` to the end of a reported field definition."""
    return definition[definition.index(" PERMISSIONS ") + 1 :]


def _model_side(current: FieldState, **change: Any) -> FieldState:
    """What a model describing *current* with *change* applied introspects to."""
    return FieldState(**({attr: getattr(current, attr) for attr in _MODELLED} | change))


async def _introspected(table: str, name: str) -> FieldState:
    state = await DatabaseIntrospector().introspect()
    return state.tables[table].fields[name]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "definition", "change"),
    [
        pytest.param("sel", "TYPE int PERMISSIONS FOR select NONE", {"field_type": "float"}, id="select-none"),
        pytest.param(
            "cond",
            "TYPE string PERMISSIONS FOR select WHERE type = 'public'",
            {"assertion": "string::len($value) > 0"},
            id="condition-naming-type",
        ),
        pytest.param(
            "n", "TYPE int ASSERT $value > 0 PERMISSIONS FOR select NONE", {"assertion": "$value >= 0"}, id="assert-gt"
        ),
        pytest.param(
            "a",
            "TYPE string PERMISSIONS FOR select WHERE string::contains($value, 'x, y') AND type = \"it's\"",
            {"assertion": "$value != NONE"},
            id="comma-in-call-and-double-quoted-apostrophe",
        ),
        pytest.param(
            "b",
            "TYPE int PERMISSIONS FOR select, create FULL, FOR update WHERE `value` > 0",
            {"field_type": "float"},
            id="full-group-first-and-backticked-value",
        ),
        pytest.param(
            "c",
            "TYPE string PERMISSIONS FOR select WHERE $auth.id = id, FOR create NONE, FOR update FULL",
            {"assertion": "$value != NONE"},
            id="condition-none-and-full",
        ),
        pytest.param(
            "p",
            "TYPE string VALUE permissions ?? 'x' PERMISSIONS FOR select NONE",
            {"assertion": "$value != NONE"},
            id="value-naming-permissions",
        ),
    ],
)
async def test_alter_and_rollback_keep_the_reported_clause(name: str, definition: str, change: dict[str, Any]) -> None:
    """The clause reads the same before the alter, after it, and after the rollback."""
    await _run(f"DEFINE TABLE {ALT} SCHEMALESS; DEFINE FIELD {name} ON {ALT} {definition};")
    before = await _definition(ALT, name)

    current = await _introspected(ALT, name)
    op = AlterField.from_field_states(ALT, current, _model_side(current, **change))

    await _run(op.forwards())
    altered = await _definition(ALT, name)
    await _run(op.backwards())
    restored = await _definition(ALT, name)

    assert altered != before, "the alter changed nothing; the comparisons below would be vacuous"
    assert _clause(altered) == _clause(before), op.forwards()
    assert _clause(restored) == _clause(before), op.backwards()
    assert restored == before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_permissions_none_still_denies_everything() -> None:
    """``PERMISSIONS NONE`` comes back spelled per action, but still denies all three.

    The server reports ``FOR select, create, update NONE`` for the re-emitted
    groups: a field has no ``delete`` permission, so the two are the same rule.
    """
    denied = ("PERMISSIONS NONE", "PERMISSIONS FOR select, create, update NONE")
    await _run(f"DEFINE TABLE {ALT} SCHEMALESS; DEFINE FIELD pn ON {ALT} TYPE string PERMISSIONS NONE;")
    before = await _definition(ALT, "pn")

    current = await _introspected(ALT, "pn")
    op = AlterField.from_field_states(ALT, current, _model_side(current, field_type="int"))

    await _run(op.forwards())
    altered = await _definition(ALT, "pn")
    await _run(op.backwards())
    restored = await _definition(ALT, "pn")

    assert _clause(before) in denied
    assert _clause(altered) in denied, op.forwards()
    assert _clause(restored) in denied, op.backwards()
    assert "TYPE int" in altered
    assert "TYPE string" in restored


def _on_doc(operations: list[Operation]) -> list[Operation]:
    return [op for op in operations if getattr(op, "table", getattr(op, "name", None)) == DOC]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_schema_diff_keeps_hand_defined_permissions_and_converges() -> None:
    """A hand-defined field permission survives the diff, and a second diff is empty.

    v2 models cannot declare an ASSERT, so the first diff proposes dropping
    ``n``'s — that ``AlterField`` must carry the permission. The rollback then
    restores both, and a model state carrying the same assertion converges:
    the ``>`` no longer hides the PERMISSIONS clause inside the assertion.
    """
    state = introspect_models([I259Doc]).tables[DOC]
    await _run(CreateTable(name=DOC, schema_mode=state.schema_mode, permissions=state.permissions or None).forwards())
    for field_name in ("title", "n"):
        await _run(AddField.from_field_state(DOC, state.fields[field_name]).forwards())
    # Outside the ORM, as an operator would.
    await _run(
        f"DEFINE FIELD OVERWRITE title ON {DOC} TYPE string PERMISSIONS FOR select WHERE type = 'public'; "
        f"DEFINE FIELD OVERWRITE n ON {DOC} TYPE int ASSERT $value > 0 PERMISSIONS FOR select NONE;"
    )
    before = {f: await _definition(DOC, f) for f in ("title", "n")}

    first = _on_doc(await schema_diff(models=[I259Doc]))

    assert [op for op in first if getattr(op, "name", None) == "title"] == []
    altered_n = [op for op in first if isinstance(op, AlterField) and op.name == "n"]
    assert len(altered_n) == 1, first
    assert altered_n[0].previous_assertion == "$value > 0"
    assert altered_n[0].permissions == {"select": "NONE"}
    assert altered_n[0].previous_permissions == {"select": "NONE"}

    for op in first:
        await _run(op.forwards())
    applied = {f: await _definition(DOC, f) for f in ("title", "n")}

    assert applied["title"] == before["title"]
    assert "ASSERT" not in applied["n"]
    assert _clause(applied["n"]) == _clause(before["n"])
    assert "extra" in (await DatabaseIntrospector().introspect()).tables[DOC].fields

    second = _on_doc(await schema_diff(models=[I259Doc]))

    assert second == []

    for op in reversed(first):
        await _run(op.backwards())
    restored = {f: await _definition(DOC, f) for f in ("title", "n")}

    assert restored == before

    # A model that could declare the assertion: the same diff, nothing to do.
    model_state = introspect_models([I259Doc])
    model_state.tables[DOC].fields.pop("extra")
    model_state.tables[DOC].fields["n"].assertion = "$value > 0"
    db_state = await DatabaseIntrospector().introspect()

    assert _on_doc(db_state.diff(model_state)) == []
