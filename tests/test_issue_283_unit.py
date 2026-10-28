"""
Unit tests for issue #283 — string literals in migration DDL.

The renderers quoted a ``COMMENT`` or a string ``DEFAULT`` by doubling its
apostrophes, as SQL does. SurrealQL escapes with a backslash: ``'it''s'`` is a
parse error, and an unescaped ``\\`` starts an escape sequence, so ``C:\\temp``
was stored with a tab in it. Reading back had the mirror defect: the server
reports a string that contains an apostrophe in double quotes, with ``\\"`` and
``\\\\`` escapes, and the parser kept the quotes.

The live-server half is ``tests/test_issue_283_integration.py``.
"""

import pytest

from src.surreal_orm import utils
from src.surreal_orm.migrations.define_parser import parse_define_field, parse_define_table
from src.surreal_orm.migrations.operations import (
    AddField,
    AlterField,
    CreateIndex,
    CreateTable,
    DefineAccess,
    DefineApi,
    DefineBearerAccess,
    DefineEvent,
)
from src.surreal_orm.migrations.state import FieldState
from src.surreal_orm.utils import parse_surql_string, surql_string

#: Each text, and the literal SurrealDB reads back as that text.
TEXTS = [
    ("it's", "'it\\'s'"),
    ('say "hi"', "'say \"hi\"'"),
    ('it\'s "x"', "'it\\'s \"x\"'"),
    ("C:\\temp", "'C:\\\\temp'"),
    # A backslash right before the closing quote must not escape it.
    ("C:\\", "'C:\\\\'"),
    ("\\'", "'\\\\\\''"),
]


# ------------------------------------------------------------------ the helpers


@pytest.mark.parametrize(("text", "literal"), TEXTS)
def test_surql_string_escapes_backslash_then_apostrophe(text: str, literal: str) -> None:
    assert surql_string(text) == literal


@pytest.mark.parametrize(("text", "literal"), TEXTS)
def test_parse_surql_string_reads_the_rendered_literal_back(text: str, literal: str) -> None:
    assert parse_surql_string(literal) == text


@pytest.mark.parametrize(
    ("reported", "text"),
    [
        # Forms SurrealDB 3.3.4 and 2.7.0 report in INFO FOR TABLE / INFO FOR DB.
        ('"it\'s"', "it's"),
        ('"it\'s \\"x\\""', 'it\'s "x"'),
        ("'say \"hi\"'", 'say "hi"'),
        ("'C:\\\\temp'", "C:\\temp"),
        ("'a\\nb\\tc'", "a\nb\tc"),
        ('"it\'s\\\\"', "it's\\"),
    ],
)
def test_parse_surql_string_reads_both_quote_styles(reported: str, text: str) -> None:
    assert parse_surql_string(reported) == text


def test_parse_surql_string_leaves_an_unquoted_value_alone() -> None:
    assert parse_surql_string("time::now()") == "time::now()"


def test_escape_single_quotes_uses_the_backslash_escape() -> None:
    assert utils.escape_single_quotes("it's C:\\temp") == "it\\'s C:\\\\temp"
    assert "''" not in (utils.escape_single_quotes.__doc__ or "")


# ------------------------------------------------------------------ renderers


@pytest.mark.parametrize(("text", "literal"), TEXTS)
def test_field_comment(text: str, literal: str) -> None:
    sql = AddField(table="t", name="f", field_type="string", comment=text).forwards()
    assert sql.endswith(f" COMMENT {literal};")


@pytest.mark.parametrize(("text", "literal"), TEXTS)
def test_field_string_default(text: str, literal: str) -> None:
    sql = AddField(table="t", name="f", field_type="string", default=text).forwards()
    assert f" DEFAULT {literal}" in sql


@pytest.mark.parametrize(("text", "literal"), TEXTS)
def test_alter_field_comment_both_ways(text: str, literal: str) -> None:
    op = AlterField.from_field_states(
        "t",
        FieldState(name="f", field_type="string", comment=text),
        FieldState(name="f", field_type="string", comment=text + "!"),
    )
    assert f"COMMENT {surql_string(text + '!')}" in op.forwards()
    assert f"COMMENT {literal}" in op.backwards()


@pytest.mark.parametrize(("text", "literal"), TEXTS)
def test_table_comment(text: str, literal: str) -> None:
    sql = CreateTable(name="t", schema_mode="SCHEMALESS", comment=text).forwards()
    assert f" COMMENT {literal}" in sql


@pytest.mark.parametrize(("text", "literal"), TEXTS)
@pytest.mark.parametrize(
    "build",
    [
        lambda c: CreateIndex(table="t", name="i", fields=["f"], comment=c),
        lambda c: DefineAccess(name="a", table="t", signup_fields={}, signin_where="true", comment=c),
        lambda c: DefineBearerAccess(name="b", comment=c),
        lambda c: DefineEvent(name="e", table="t", when="true", then="RETURN 1", comment=c),
        lambda c: DefineApi(name="/x", method="GET", handler="RETURN 1", comment=c),
    ],
    ids=["CreateIndex", "DefineAccess", "DefineBearerAccess", "DefineEvent", "DefineApi"],
)
def test_operation_comment(build: object, text: str, literal: str) -> None:
    sql = build(text).forwards()  # type: ignore[operator]
    assert sql.endswith(f"COMMENT {literal};"), sql


# ------------------------------------------------------------------ parsers


@pytest.mark.parametrize(
    ("reported", "text"),
    [
        ('"it\'s"', "it's"),
        ('"it\'s \\"x\\""', 'it\'s "x"'),
        ("'say \"hi\"'", 'say "hi"'),
        ("'C:\\\\temp'", "C:\\temp"),
    ],
)
def test_field_comment_is_read_back(reported: str, text: str) -> None:
    state = parse_define_field(f"DEFINE FIELD f ON t TYPE string COMMENT {reported} PERMISSIONS FULL")
    assert state.comment == text


@pytest.mark.parametrize(
    ("reported", "text"),
    [
        ('"it\'s"', "it's"),
        ('"it\'s \\"x\\""', 'it\'s "x"'),
        ("'say \"hi\"'", 'say "hi"'),
        ("'C:\\\\temp'", "C:\\temp"),
        ("'C:\\\\'", "C:\\"),
    ],
)
def test_table_comment_is_read_back(reported: str, text: str) -> None:
    parsed = parse_define_table(f"DEFINE TABLE t TYPE ANY SCHEMALESS COMMENT {reported} PERMISSIONS FOR select FULL")
    assert parsed["comment"] == text
    assert parsed["permissions"] == {"select": "FULL"}


@pytest.mark.parametrize(
    ("reported", "text"),
    [
        ('"it\'s \\"x\\""', 'it\'s "x"'),
        ("'say \\'hi\\''", "say 'hi'"),
        ("'C:\\\\temp'", "C:\\temp"),
    ],
)
def test_quoted_default_is_read_back(reported: str, text: str) -> None:
    state = parse_define_field(f"DEFINE FIELD f ON t TYPE string DEFAULT {reported} PERMISSIONS FULL")
    assert state.default == text


@pytest.mark.parametrize(("text", "literal"), TEXTS)
def test_rendered_field_round_trips_through_the_parser(text: str, literal: str) -> None:
    sql = AddField(table="t", name="f", field_type="string", default=text, comment=text).forwards()
    state = parse_define_field(sql.rstrip(";"))
    assert (state.default, state.comment) == (text, text)
