"""
Unit tests for #568 on the v2 (SurrealDB 2.x) line: DEFINE clauses are read
with one SurrealQL scanner.

Two defects made ``schema_diff()`` / ``makemigrations`` report the same fields
and tables as changed on every run:

* ``_consume_until_keyword()`` counted every ``<`` and ``>`` as an angle
  bracket, so a comparison (``ASSERT $value > 0``) never closed and the
  clauses after it (``COMMENT 'positive'``) were folded into the expression.
* ``_split_permission_groups()`` tracked single quotes only, so a
  double-quoted ``"it's"`` in a permission condition opened a quote that ran
  to the end of the clause and swallowed every later ``FOR`` group.

Every statement below is written as SurrealDB 2.7.0 reports it from
``INFO FOR TABLE`` / ``INFO FOR DB``.
"""

import pytest

from src.surreal_orm.migrations.define_parser import parse_define_field, parse_define_table

# -- comparisons in VALUE / ASSERT --------------------------------------------


@pytest.mark.parametrize(
    "assertion",
    [
        "$value > 0",
        "$value < 10",
        "$value <= 10",
        "$value >= 1",
        "$value != 5",
        "$value <= 10 AND $value >= 1 AND $value != 5",
        "$value / 2 > 1",
    ],
)
def test_comparison_in_assert_does_not_swallow_comment(assertion: str) -> None:
    field = parse_define_field(
        f"DEFINE FIELD n ON fu TYPE int DEFAULT 1 VALUE $value ASSERT {assertion} COMMENT 'positive' PERMISSIONS FULL"
    )
    assert field.assertion == assertion
    assert field.comment == "positive"
    assert field.value == "$value"
    assert field.default == 1
    assert field.field_type == "int"


def test_comparison_in_value_if_else_does_not_swallow_comment() -> None:
    field = parse_define_field(
        "DEFINE FIELD c ON fu TYPE int VALUE IF $value > 5 { 5 } ELSE { $value } COMMENT 'clamp' PERMISSIONS FULL"
    )
    assert field.value == "IF $value > 5 { 5 } ELSE { $value }"
    assert field.comment == "clamp"


def test_less_than_before_assert_keeps_assert_clause() -> None:
    field = parse_define_field("DEFINE FIELD v ON fu TYPE int VALUE $value < 3 ASSERT $value > 0 PERMISSIONS FULL")
    assert field.value == "$value < 3"
    assert field.assertion == "$value > 0"


def test_graph_arrows_are_not_brackets() -> None:
    field = parse_define_field(
        "DEFINE FIELD f ON fu TYPE any VALUE ->likes->post ASSERT count(<-follows) > 0 COMMENT 'x' PERMISSIONS FULL"
    )
    assert field.value == "->likes->post"
    assert field.assertion == "count(<-follows) > 0"
    assert field.comment == "x"


# -- types and casts with angle brackets --------------------------------------


def test_parameterised_types_still_parse() -> None:
    field = parse_define_field("DEFINE FIELD tags ON fu TYPE array<string> COMMENT 'tags' PERMISSIONS FULL")
    assert field.field_type == "array<string>"
    assert field.comment == "tags"

    nested = parse_define_field("DEFINE FIELD owner ON fu TYPE option<record<user>> PERMISSIONS FULL")
    assert nested.field_type == "record<user>"
    assert nested.nullable is True


def test_cast_and_nested_type_still_parse() -> None:
    field = parse_define_field(
        "DEFINE FIELD d ON fu TYPE option<array<record<user>>> VALUE <array> $value COMMENT 'cast' PERMISSIONS FULL"
    )
    assert field.field_type == "array<record<user>>"
    assert field.nullable is True
    assert field.value == "<array> $value"
    assert field.comment == "cast"


def test_cast_inside_comparison() -> None:
    field = parse_define_field(
        "DEFINE FIELD k ON fu TYPE int VALUE <int> $value ASSERT <int> $value > 1 COMMENT 'half' PERMISSIONS FULL"
    )
    assert field.value == "<int> $value"
    assert field.assertion == "<int> $value > 1"
    assert field.comment == "half"


def test_type_with_keyword_named_table_is_one_bracket() -> None:
    # A table named like a clause keyword inside the type brackets stays in the type.
    field = parse_define_field("DEFINE FIELD r ON fu TYPE record<user | comment> COMMENT 'r' PERMISSIONS FULL")
    assert field.field_type == "record<user | comment>"
    assert field.comment == "r"


# -- double-quoted strings in permission conditions ---------------------------


def test_table_permission_with_double_quoted_apostrophe() -> None:
    table = parse_define_table(
        'DEFINE TABLE fu TYPE NORMAL SCHEMAFULL PERMISSIONS FOR select WHERE name = "it\'s" OR $auth.level > 1, '
        "FOR create, update, delete NONE"
    )
    assert table["permissions"] == {
        "select": 'name = "it\'s" OR $auth.level > 1',
        "create": "NONE",
        "update": "NONE",
        "delete": "NONE",
    }
    assert table["schema_mode"] == "SCHEMAFULL"


def test_table_comment_before_permission_with_comparison() -> None:
    table = parse_define_table(
        "DEFINE TABLE fv TYPE NORMAL SCHEMAFULL COMMENT 'x' PERMISSIONS FOR select WHERE $auth.level > 1, "
        "FOR create, update, delete NONE"
    )
    assert table["comment"] == "x"
    assert table["permissions"]["select"] == "$auth.level > 1"
    assert table["permissions"]["delete"] == "NONE"


def test_field_permission_with_double_quoted_apostrophe() -> None:
    field = parse_define_field(
        'DEFINE FIELD e ON fu TYPE string PERMISSIONS FOR select WHERE name = "it\'s", FOR create, update NONE'
    )
    assert field.permissions == {"select": 'name = "it\'s"', "create": "NONE", "update": "NONE"}


# -- regex literals (2.x escape rule) -----------------------------------------


def test_quote_inside_regex_does_not_open_a_string() -> None:
    field = parse_define_field(
        "DEFINE FIELD name ON fw TYPE string ASSERT $value = /^[A-Za-z' -]+$/ COMMENT 'nm' "
        'PERMISSIONS FOR select WHERE name = /^[^"]+$/, FOR create, update NONE'
    )
    assert field.assertion == "$value = /^[A-Za-z' -]+$/"
    assert field.comment == "nm"
    assert field.permissions == {"select": 'name = /^[^"]+$/', "create": "NONE", "update": "NONE"}


def test_table_permission_with_quote_inside_regex() -> None:
    table = parse_define_table(
        "DEFINE TABLE fw TYPE NORMAL SCHEMAFULL PERMISSIONS FOR select WHERE name = /it's/, FOR create, update, delete NONE"
    )
    assert table["permissions"] == {
        "select": "name = /it's/",
        "create": "NONE",
        "update": "NONE",
        "delete": "NONE",
    }


def test_regex_escaped_slash_keeps_2x_rule() -> None:
    # Only ``\/`` is an escape in a 2.x pattern: the literal ends at the next bare ``/``.
    field = parse_define_field(r"DEFINE FIELD p ON fu TYPE string ASSERT $value = /a\/'b/ COMMENT 'p' PERMISSIONS FULL")
    assert field.assertion == r"$value = /a\/'b/"
    assert field.comment == "p"


def test_regex_opening_with_a_space_after_the_keyword() -> None:
    # 2.7.0 reports this pattern as written: a ``/`` right after ASSERT opens one.
    field = parse_define_field("DEFINE FIELD s ON fu TYPE string ASSERT / it's/ = $value COMMENT 'sp' PERMISSIONS FULL")
    assert field.assertion == "/ it's/ = $value"
    assert field.comment == "sp"


def test_division_is_not_a_regex() -> None:
    field = parse_define_field(
        "DEFINE FIELD h ON fu TYPE int VALUE $value / 2 ASSERT $value / 2 > 1 COMMENT 'h/2' PERMISSIONS FULL"
    )
    assert field.value == "$value / 2"
    assert field.assertion == "$value / 2 > 1"
    assert field.comment == "h/2"
