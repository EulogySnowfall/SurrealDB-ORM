"""
Unit tests for #558 on the v2 (SurrealDB 2.x) line: a regex literal after a
word operator keeps a PERMISSIONS start inside it.

#517 (#554 on this line) skips a candidate PERMISSIONS start when the text
before it ends inside a pattern whose opening ``/`` is followed by a
non-space character, but only where an operand is expected: after a symbol
operator, an opening bracket or ``ASSERT``/``VALUE``/``DEFAULT``. A word
operator (``CONTAINS``, ``IN``, ``AND``, ...) was not one of them, so
``ASSERT $value CONTAINS /x PERMISSIONS FULL / y/`` was cut at the
PERMISSIONS inside the pattern. SurrealDB 2.7.0 accepts a pattern operand
after every word operator listed here.
"""

import pytest

from src.surreal_orm.migrations.define_parser import parse_define_field
from src.surreal_orm.surreal_function import SurrealFunc

_WORD_OPERATORS = [
    "CONTAINS",
    "CONTAINSNOT",
    "CONTAINSALL",
    "CONTAINSANY",
    "CONTAINSNONE",
    "INSIDE",
    "NOTINSIDE",
    "ALLINSIDE",
    "ANYINSIDE",
    "NONEINSIDE",
    "OUTSIDE",
    "INTERSECTS",
    "IN",
    "NOT IN",
    "NOT  IN",
    "IS",
    "IS NOT",
    "AND",
    "OR",
    "contains",
    "Inside",
    "not in",
    "or",
]


class TestI558RegexAfterWordOperator:
    """A pattern opened right after a word operator is not split at a PERMISSIONS inside it."""

    @pytest.mark.parametrize("operator", _WORD_OPERATORS)
    def test_the_pattern_stays_in_the_assertion(self, operator: str) -> None:
        result = parse_define_field(f"DEFINE FIELD f ON t ASSERT $value {operator} /x PERMISSIONS FULL / y/")

        assert result.assertion == f"$value {operator} /x PERMISSIONS FULL / y/"
        assert result.permissions == {}

    @pytest.mark.parametrize("operator", _WORD_OPERATORS)
    def test_a_real_clause_after_the_pattern_is_still_found(self, operator: str) -> None:
        result = parse_define_field(
            f"DEFINE FIELD f ON t ASSERT $value {operator} /x PERMISSIONS FULL / y/ PERMISSIONS FOR select NONE"
        )

        assert result.assertion == f"$value {operator} /x PERMISSIONS FULL / y/"
        assert result.permissions == {"select": "NONE"}

    @pytest.mark.parametrize("keyword", ["THEN", "ELSE"])
    def test_a_pattern_after_then_or_else_stays_in_the_value(self, keyword: str) -> None:
        branches = "THEN /x PERMISSIONS FULL / y/ ELSE 1" if keyword == "THEN" else "THEN 1 ELSE /x PERMISSIONS FULL / y/"
        result = parse_define_field(f"DEFINE FIELD f ON t VALUE IF $value {branches} END")

        assert result.value == f"IF $value {branches} END"
        assert result.permissions == {}

    def test_a_pattern_after_return_stays_in_the_value(self) -> None:
        result = parse_define_field("DEFINE FIELD f ON t VALUE { RETURN /x PERMISSIONS FULL / y/ }")

        assert result.value == "{ RETURN /x PERMISSIONS FULL / y/ }"
        assert result.permissions == {}

    def test_server_output_after_a_word_operator_keeps_its_clause(self) -> None:
        """Exact SurrealDB 2.7.0 ``INFO FOR TABLE`` output for ``ASSERT $value CONTAINS /x PERMISSIONS FULL y/``."""
        result = parse_define_field("DEFINE FIELD w1 ON t558b ASSERT $value CONTAINS /x PERMISSIONS FULL y/ PERMISSIONS FULL")

        assert result.assertion == "$value CONTAINS /x PERMISSIONS FULL y/"
        assert result.permissions == {}


class TestI558DivisionStillRead:
    """An operand on the left of an unspaced ``/`` still makes it a division."""

    def test_an_unspaced_division_in_an_assertion(self) -> None:
        result = parse_define_field("DEFINE FIELD a ON t TYPE int ASSERT $value/2 > 1 PERMISSIONS FULL")

        assert result.assertion == "$value/2 > 1"
        assert result.permissions == {}

    def test_an_unspaced_division_in_an_assertion_with_a_clause(self) -> None:
        result = parse_define_field("DEFINE FIELD a ON t TYPE int ASSERT $value/2 > 1 PERMISSIONS FOR select NONE")

        assert result.assertion == "$value/2 > 1"
        assert result.permissions == {"select": "NONE"}

    def test_an_unspaced_division_of_numbers(self) -> None:
        result = parse_define_field("DEFINE FIELD a ON t TYPE float ASSERT 1/2 < $value PERMISSIONS FOR select NONE")

        assert result.assertion == "1/2 < $value"
        assert result.permissions == {"select": "NONE"}

    def test_an_unspaced_division_in_a_default(self) -> None:
        result = parse_define_field("DEFINE FIELD a ON t TYPE float DEFAULT 1/2 PERMISSIONS FULL")

        assert result.default == SurrealFunc("1/2")
        assert result.permissions == {}

    @pytest.mark.parametrize(
        "operand",
        [
            pytest.param("$value.in", id="path-ending-in-in"),
            pytest.param("$value.contains", id="path-ending-in-contains"),
            pytest.param("contains_x", id="name-starting-with-contains"),
            pytest.param("x_in", id="name-ending-in-in"),
            pytest.param("$value.and", id="path-ending-in-and"),
        ],
    )
    def test_a_name_ending_in_an_operator_word_is_an_operand(self, operand: str) -> None:
        """No whitespace before the word, or the word is part of a longer name: the ``/`` divides."""
        result = parse_define_field(f"DEFINE FIELD a ON t TYPE int ASSERT {operand} /2 > 1 PERMISSIONS FOR select NONE")

        assert result.assertion == f"{operand} /2 > 1"
        assert result.permissions == {"select": "NONE"}

    @pytest.mark.parametrize("operator", ["CONTAINS", "IN", "AND"])
    def test_an_unspaced_slash_after_the_word_is_a_division(self, operator: str) -> None:
        """No whitespace between the word and the ``/``: the word is read as a name, as before."""
        result = parse_define_field(f"DEFINE FIELD a ON t TYPE int ASSERT $value {operator}/2 > 1 PERMISSIONS FOR select NONE")

        assert result.assertion == f"$value {operator}/2 > 1"
        assert result.permissions == {"select": "NONE"}
