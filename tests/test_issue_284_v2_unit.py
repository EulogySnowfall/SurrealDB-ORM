"""
Unit tests for #284 on the v2 line: text in migration DDL that does not make the round trip.

Every migration renderer quoted a COMMENT or a string DEFAULT by doubling the
apostrophe (``'it''s'``). SurrealDB 2.x does not read ``''`` as an escaped
quote: it ends the literal there and the statement fails to parse. A backslash
was not escaped at all, so ``C:\\temp`` reached the server as ``C:<TAB>emp``.
The literal is now rendered the way the server's lexer reads it (``\\\\`` for a
backslash, ``\\'`` for a quote), by one shared helper.

On the way back, the server reports a text containing an apostrophe
double-quoted (``COMMENT "it's \\"x\\""``) and escapes backslashes. The parser
only knew single quotes and ``''``, so it kept the quotes, kept the escapes and,
because the apostrophe inside the double-quoted text opened a "quote" that
never closed, swallowed every clause after it.

Run with: pytest tests/test_issue_284_v2_unit.py
"""

import pytest

from src.surreal_orm.migrations.define_parser import (
    _parse_default_value,
    _unquote_surql_string,
    parse_define_field,
    parse_define_table,
)
from src.surreal_orm.migrations.operations import (
    CreateIndex,
    DefineAccess,
    DefineEvent,
    _render_default,
    _render_define_field,
    _render_define_table,
)
from src.surreal_orm.utils import escape_single_quotes, surql_string_literal

#: The texts of the acceptance criteria, plus a mix of everything.
TEXTS = [
    "it's",
    'say "hi"',
    'it\'s "x"',
    "C:\\temp",
    "a\\' ; b -- c",
    "plain",
]

#: How SurrealDB 2.7.0 lexes the single-quoted literals the ORM renders.
_SERVER_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t", "r": "\r", "/": "/"}


def _server_reads(literal: str) -> str:
    """Decode a single-quoted SurrealQL literal the way the 2.x lexer does.

    Deliberately independent of the parser under test: a ``'`` before the
    closing quote ends the literal (so ``'it''s'`` is two tokens, rejected),
    and a backslash escapes the next character.
    """
    assert literal[0] == "'" and literal[-1] == "'", literal
    out: list[str] = []
    i = 1
    while i < len(literal) - 1:
        ch = literal[i]
        if ch == "\\":
            out.append(_SERVER_ESCAPES[literal[i + 1]])
            i += 2
            continue
        assert ch != "'", f"the literal ends early at {i}: {literal}"
        out.append(ch)
        i += 1
    return "".join(out)


def _comment_literal(statement: str) -> str:
    """The quoted literal following ``COMMENT `` in a rendered statement."""
    start = statement.index("COMMENT ") + len("COMMENT ")
    end = statement.rindex("'") + 1
    return statement[start:end]


class TestStringLiteral:
    @pytest.mark.parametrize("text", TEXTS)
    def test_literal_reads_back_unchanged(self, text: str) -> None:
        assert _server_reads(surql_string_literal(text)) == text

    def test_apostrophe_is_backslash_escaped_not_doubled(self) -> None:
        assert surql_string_literal("it's") == "'it\\'s'"

    def test_backslash_is_escaped_before_the_quote(self) -> None:
        # Order matters: escaping the quote first would double its backslash.
        assert surql_string_literal("a\\'") == "'a\\\\\\''"

    def test_escape_single_quotes_uses_the_same_rules(self) -> None:
        assert escape_single_quotes("it's C:\\temp") == "it\\'s C:\\\\temp"


class TestRenderers:
    @pytest.mark.parametrize("text", TEXTS)
    def test_render_default(self, text: str) -> None:
        assert _server_reads(_render_default(text)) == text

    @pytest.mark.parametrize("text", TEXTS)
    def test_render_define_field_comment(self, text: str) -> None:
        sql = _render_define_field("t", "f", overwrite=False, field_type="string", comment=text)
        assert _server_reads(_comment_literal(sql)) == text

    @pytest.mark.parametrize("text", TEXTS)
    def test_render_define_field_default(self, text: str) -> None:
        sql = _render_define_field("t", "f", overwrite=False, field_type="string", default=text)
        literal = sql[sql.index("DEFAULT ") + len("DEFAULT ") : -1]
        assert _server_reads(literal) == text

    @pytest.mark.parametrize("text", TEXTS)
    def test_render_define_table(self, text: str) -> None:
        sql = _render_define_table("t", overwrite=False, schema_mode="SCHEMAFULL", comment=text)
        assert _server_reads(_comment_literal(sql)) == text

    @pytest.mark.parametrize("text", TEXTS)
    def test_create_index(self, text: str) -> None:
        sql = CreateIndex(table="t", name="i", fields=["a"], comment=text).forwards()
        assert _server_reads(_comment_literal(sql)) == text

    @pytest.mark.parametrize("text", TEXTS)
    def test_define_access(self, text: str) -> None:
        sql = DefineAccess(
            name="acc",
            table="users",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
            comment=text,
        ).forwards()
        assert _server_reads(_comment_literal(sql)) == text

    @pytest.mark.parametrize("text", TEXTS)
    def test_define_event(self, text: str) -> None:
        sql = DefineEvent(name="e", table="t", when="true", then="CREATE log", comment=text).forwards()
        assert _server_reads(_comment_literal(sql)) == text


class TestParser:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param('"it\'s"', "it's", id="double-quoted-apostrophe"),
            pytest.param('"it\'s \\"x\\""', 'it\'s "x"', id="double-quoted-both"),
            pytest.param("'C:\\\\temp'", "C:\\temp", id="backslash"),
            pytest.param("'say \"hi\"'", 'say "hi"', id="single-quoted-double"),
            pytest.param("'line1\\nline2\\ttab'", "line1\nline2\ttab", id="control-escapes"),
            pytest.param("'it\\'s'", "it's", id="single-quoted-escaped"),
        ],
    )
    def test_unquote(self, raw: str, expected: str) -> None:
        assert _unquote_surql_string(raw) == expected

    @pytest.mark.parametrize(
        ("clause", "expected"),
        [
            pytest.param('"it\'s"', "it's", id="apostrophe"),
            pytest.param('"it\'s \\"x\\""', 'it\'s "x"', id="both-quotes"),
            pytest.param("'C:\\\\temp'", "C:\\temp", id="backslash"),
            pytest.param('"a\\\\\' ; DEFINE TABLE b; -- c"', "a\\' ; DEFINE TABLE b; -- c", id="mixed"),
        ],
    )
    def test_field_comment(self, clause: str, expected: str) -> None:
        parsed = parse_define_field(f"DEFINE FIELD f ON t TYPE string COMMENT {clause} PERMISSIONS FOR select NONE")
        assert parsed.comment == expected
        # The clause after the comment is still read.
        assert parsed.permissions == {"select": "NONE"}

    @pytest.mark.parametrize(
        ("clause", "expected"),
        [
            pytest.param('"it\'s"', "it's", id="apostrophe"),
            pytest.param('"it\'s \\"x\\""', 'it\'s "x"', id="both-quotes"),
            pytest.param("'C:\\\\temp'", "C:\\temp", id="backslash"),
        ],
    )
    def test_table_comment(self, clause: str, expected: str) -> None:
        parsed = parse_define_table(f"DEFINE TABLE t TYPE ANY SCHEMAFULL COMMENT {clause} PERMISSIONS FOR select FULL")
        assert parsed["comment"] == expected
        assert parsed["schema_mode"] == "SCHEMAFULL"
        assert parsed["permissions"] == {"select": "FULL"}

    def test_default_value(self) -> None:
        assert _parse_default_value('"it\'s \\"x\\""') == 'it\'s "x"'
        assert _parse_default_value("'C:\\\\temp'") == "C:\\temp"

    def test_default_from_field_definition(self) -> None:
        parsed = parse_define_field('DEFINE FIELD f ON t TYPE string DEFAULT "it\'s \\"x\\"" PERMISSIONS FULL')
        assert parsed.default == 'it\'s "x"'

    @pytest.mark.parametrize("text", TEXTS)
    def test_render_then_parse_round_trips(self, text: str) -> None:
        field = parse_define_field(
            _render_define_field("t", "f", overwrite=False, field_type="string", default=text, comment=text)
        )
        assert (field.comment, field.default) == (text, text)
        table = parse_define_table(_render_define_table("t", overwrite=False, schema_mode="SCHEMALESS", comment=text))
        assert table["comment"] == text
