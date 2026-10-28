"""
Unit tests for issue #401 — a text DEFAULT is always a text, and highlight tags are string literals.

A ``str`` default containing ``::`` or starting with ``$`` was emitted unquoted,
so a text read back from the database (``AlterField.backwards()``,
``from_field_states()``) or a model default was replayed as SurrealQL rather
than stored as the text it is. Server-side expressions are carried as
``SurrealFunc``; only a whole no-argument call or a bare ``$identifier`` stays
accepted as a ``str`` expression, with a ``DeprecationWarning``.

``SearchHighlight`` escaped the apostrophe but not the backslash, so a tag
ending in a backslash did not render as the text it holds.
"""

import warnings

import pytest

from src.surreal_orm import SurrealFunc
from src.surreal_orm.migrations.define_parser import _parse_default_value, parse_define_field
from src.surreal_orm.migrations.generator import MigrationGenerator
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
from src.surreal_orm.migrations.operations import AddField, AlterField
from src.surreal_orm.migrations.state import FieldState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.search import SearchHighlight
from src.surreal_orm.utils import parse_surql_string, surql_string

WITNESS = "x::y; DEFINE TABLE witness; --"

#: Texts that look like an expression but are not one.
TEXTS = [
    WITNESS,
    "fr::CA",
    "$abc::d",
    "$value; DEFINE TABLE witness",
    "time::now(); DEFINE TABLE witness",
    "string::concat('a', 'b')",
    "time::now() ",
    "$a b",
    "$é",
]

#: Strings still accepted as a legacy expression default.
LEGACY_EXPRESSIONS = ["time::now()", "rand::uuid()", "rand::uuid::v7()", "$value", "$before"]


# ------------------------------------------------------------------ rendering


@pytest.mark.parametrize("text", TEXTS)
def test_add_field_quotes_a_text_default(text: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        sql = AddField(table="t", name="f", field_type="string", default=text).forwards()
    assert f"DEFAULT {surql_string(text)}" in sql, sql


@pytest.mark.parametrize("text", TEXTS)
def test_alter_field_quotes_a_text_default_both_ways(text: str) -> None:
    op = AlterField(table="t", name="f", field_type="string", default=text, previous_type="string", previous_default=text)
    assert f"DEFAULT {surql_string(text)}" in op.forwards()
    assert f"DEFAULT {surql_string(text)}" in op.backwards()


def test_witness_default_renders_as_one_statement() -> None:
    sql = AddField(table="t", name="f", field_type="string", default=WITNESS).forwards()
    assert sql == "DEFINE FIELD f ON t TYPE string DEFAULT 'x::y; DEFINE TABLE witness; --';"


@pytest.mark.parametrize("expression", ["time::now()", "rand::uuid()", "$value", "math::max([1, 2])"])
def test_surreal_func_default_is_emitted_as_written(expression: str) -> None:
    sql = AddField(table="t", name="f", field_type="any", default=SurrealFunc(expression)).forwards()
    assert f"DEFAULT {expression};" in sql, sql


@pytest.mark.parametrize("expression", LEGACY_EXPRESSIONS)
def test_legacy_str_expression_stays_unquoted_with_a_warning(expression: str) -> None:
    with pytest.warns(DeprecationWarning, match="SurrealFunc"):
        sql = AddField(table="t", name="f", field_type="any", default=expression).forwards()
    assert f"DEFAULT {expression};" in sql, sql


# ------------------------------------------------------------------ read back


@pytest.mark.parametrize("text", TEXTS)
def test_backwards_replays_a_text_read_from_the_database(text: str) -> None:
    current = parse_define_field(f"DEFINE FIELD f ON t TYPE string DEFAULT {surql_string(text)} PERMISSIONS FULL")
    assert current.default == text
    target = FieldState(name="f", field_type="string", nullable=False)

    op = AlterField.from_field_states("t", current, target)

    assert op.previous_default == text
    assert f"DEFAULT {surql_string(text)} DROP ASSERT" in op.backwards()


@pytest.mark.parametrize("reported", ["time::now()", "`rand`::uuid()", "$value"])
def test_unquoted_default_is_read_back_as_an_expression(reported: str) -> None:
    assert _parse_default_value(reported) == SurrealFunc(reported)


def test_backwards_replays_an_expression_read_from_the_database() -> None:
    current = parse_define_field("DEFINE FIELD f ON t TYPE datetime DEFAULT time::now() PERMISSIONS FULL")
    target = FieldState(name="f", field_type="datetime", nullable=False)

    op = AlterField.from_field_states("t", current, target)

    assert op.previous_default == SurrealFunc("time::now()")
    assert "DEFAULT time::now() " in op.backwards()


def test_reported_expression_refused_by_surreal_func_is_still_carried() -> None:
    raw = "{ DEFINE TABLE x; RETURN 1 }"
    with pytest.raises(ValueError):
        SurrealFunc(raw)
    parsed = _parse_default_value(raw)
    assert isinstance(parsed, SurrealFunc)
    assert parsed.expression == raw


# ------------------------------------------------------------------ model side


class I401Text(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i401_unit_text")

    id: str | None = None
    note: str = WITNESS


class I401Legacy(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i401_unit_legacy")

    id: str | None = None
    stamp: str = "time::now()"


def _model_field(model: type[BaseSurrealModel], name: str) -> FieldState:
    state = ModelIntrospector([model]).introspect()
    return state.tables[model.get_table_name()].fields[name]


def test_model_legacy_expression_default_is_an_expression() -> None:
    with pytest.warns(DeprecationWarning, match="SurrealFunc"):
        field = _model_field(I401Legacy, "stamp")
    assert field.default == SurrealFunc("time::now()")


def test_model_and_database_states_compare_equal() -> None:
    text_db = parse_define_field(f"DEFINE FIELD note ON i401_unit_text TYPE string DEFAULT {surql_string(WITNESS)}")
    expr_db = parse_define_field("DEFINE FIELD stamp ON i401_unit_legacy TYPE string DEFAULT time::now()")
    with pytest.warns(DeprecationWarning):
        legacy = _model_field(I401Legacy, "stamp")
    assert _model_field(I401Text, "note").default == text_db.default
    assert legacy.default == expr_db.default


# ------------------------------------------------------------------ code generation


def test_inspectdb_renders_an_expression_as_surreal_func() -> None:
    field = FieldState(name="created_at", field_type="datetime", nullable=False, default=SurrealFunc("time::now()"))
    line, imports = ModelCodeGenerator()._generate_field(field)
    assert "default=SurrealFunc('time::now()')" in line
    assert "from surreal_orm import SurrealFunc" in imports


def test_inspectdb_renders_a_text_as_a_python_string() -> None:
    field = FieldState(name="note", field_type="string", nullable=False, default=WITNESS)
    line, _ = ModelCodeGenerator()._generate_field(field)
    assert f"default={WITNESS!r}" in line


def test_migration_file_imports_surreal_func(tmp_path: object) -> None:
    op = AddField(table="t", name="f", field_type="datetime", default=SurrealFunc("time::now()"))
    content = MigrationGenerator(str(tmp_path))._render_migration("0001_x", [op], [])
    assert "from surreal_orm import SurrealFunc" in content
    assert "default=SurrealFunc('time::now()')" in content
    namespace: dict[str, object] = {}
    exec(compile(content.replace("from surreal_orm", "from src.surreal_orm"), "m.py", "exec"), namespace)
    migration = namespace["migration"]
    assert migration.operations[0].default == SurrealFunc("time::now()")  # type: ignore[attr-defined]


# ------------------------------------------------------------------ SearchHighlight


@pytest.mark.parametrize(
    "tag",
    ["a\\", "a\\'; DEFINE TABLE witness; --", "\\\\"],
)
def test_highlight_tags_are_string_literals(tag: str) -> None:
    sql = SearchHighlight(tag, tag, 0).to_surql("snippet")
    literal = surql_string(tag)
    assert sql == f"search::highlight({literal}, {literal}, 0) AS snippet"
    assert parse_surql_string(literal) == tag
