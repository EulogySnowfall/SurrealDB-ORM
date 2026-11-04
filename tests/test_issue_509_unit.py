"""
Unit tests for #509 on the v2 (SurrealDB 2.x) line: a PERMISSIONS keyword
inside a regex literal is not a field's PERMISSIONS clause.

The fallback added in #506 tries every candidate clause start from the end and
keeps the first whose remainder scans balanced. A start inside a quoted string
leaves the quote open and is skipped, but a start inside a regex literal only
sees the closing ``/`` once cut out, reads it as a division and looks balanced,
so the pattern was split in two. SurrealDB writes a division as `` / `` with
an operand after it; any other ``/`` read as one in a remainder is a pattern
end without its start, and rejects the candidate, unless an earlier `` / ``
in the remainder can be that start (``WHERE / x/``).
"""

import pytest

from src.surreal_orm.migrations.define_parser import parse_define_field


class TestI509RegexLiteral:
    """A PERMISSIONS start nested in a regex literal stays in its clause."""

    @pytest.mark.parametrize(
        ("statement", "assertion"),
        [
            pytest.param(
                "DEFINE FIELD a ON t TYPE string ASSERT $value = / PERMISSIONS FULL/",
                "$value = / PERMISSIONS FULL/",
                id="pattern-starting-with-a-space",
            ),
            pytest.param(
                "DEFINE FIELD a ON t TYPE string ASSERT $value = /x PERMISSIONS NONE/",
                "$value = /x PERMISSIONS NONE/",
                id="pattern-with-the-keyword-inside",
            ),
            pytest.param(
                "DEFINE FIELD a ON t TYPE string ASSERT $value INSIDE [/a PERMISSIONS FOR b/, /c/]",
                "$value INSIDE [/a PERMISSIONS FOR b/, /c/]",
                id="pattern-inside-brackets",
            ),
            pytest.param(
                "DEFINE FIELD a ON t TYPE string ASSERT $value = / PERMISSIONS FULL /",
                "$value = / PERMISSIONS FULL /",
                id="pattern-ending-with-a-space",
            ),
        ],
    )
    def test_no_clause_is_taken_from_a_pattern(self, statement: str, assertion: str) -> None:
        result = parse_define_field(statement)

        assert result.assertion == assertion
        assert result.permissions == {}

    def test_a_real_clause_after_such_a_pattern_is_still_found(self) -> None:
        result = parse_define_field(
            "DEFINE FIELD a ON t TYPE string ASSERT $value = / PERMISSIONS FULL/ PERMISSIONS FOR select NONE"
        )

        assert result.assertion == "$value = / PERMISSIONS FULL/"
        assert result.permissions == {"select": "NONE"}

    def test_the_server_form_of_the_reported_field(self) -> None:
        """Verbatim ``INFO FOR TABLE`` output of SurrealDB 2.7.0: the real clause comes last."""
        result = parse_define_field("DEFINE FIELD a ON r509 TYPE string ASSERT $value = / PERMISSIONS FULL/ PERMISSIONS FULL")

        assert result.assertion == "$value = / PERMISSIONS FULL/"
        assert result.permissions == {}

    def test_a_balanced_scan_does_not_rule_out_the_candidates(self) -> None:
        """Server form (#259): ``/ +/`` after ASSERT reads as a division, and ``+/`` hides the clause.

        The scan of the whole body then ends balanced without finding the
        clause, which is why it is not enough to stop there.
        """
        result = parse_define_field(
            "DEFINE FIELD b2 ON t TYPE string ASSERT / +/ != $value "
            "PERMISSIONS FOR select WHERE $auth.score / 2 > 1, FOR create, update FULL"
        )

        assert result.assertion == "/ +/ != $value"
        assert result.permissions == {"select": "$auth.score / 2 > 1"}

    def test_an_unbalanced_body_still_uses_the_fallback(self) -> None:
        """The ``/ it's/`` case: the scan ends inside the apostrophe, so the candidates are tried."""
        result = parse_define_field("DEFINE FIELD k1 ON t TYPE string ASSERT / it's/ = $value PERMISSIONS FOR select NONE")

        assert result.assertion == "/ it's/ = $value"
        assert result.permissions == {"select": "NONE"}

    def test_a_pattern_after_a_keyword_in_the_clause_keeps_the_clause(self) -> None:
        """Verbatim ``INFO FOR TABLE`` output of SurrealDB 2.7.0.

        ``WHERE / x/`` reads as a division followed by the pattern's closing
        ``/``; that ``/`` is paired with the division, not taken as the end of
        a pattern opened before the candidate clause.
        """
        result = parse_define_field(
            "DEFINE FIELD k ON r509p TYPE string ASSERT / it's/ = $value "
            "PERMISSIONS FOR select WHERE / x/ = name, FOR create, update FULL"
        )

        assert result.assertion == "/ it's/ = $value"
        assert result.permissions == {"select": "/ x/ = name"}


class TestI517SpacedSlashInPattern:
    """#517: a pattern holding a spaced slash still keeps a PERMISSIONS start inside it."""

    def test_a_spaced_slash_does_not_close_the_pattern_early(self) -> None:
        """``/ y/`` pairs as a division in the remainder; the text before the start is still inside ``/x``."""
        result = parse_define_field("DEFINE FIELD a ON t TYPE string ASSERT $value = /x PERMISSIONS FULL / y/")

        assert result.assertion == "$value = /x PERMISSIONS FULL / y/"
        assert result.permissions == {}

    def test_a_real_clause_after_such_a_pattern_is_still_found(self) -> None:
        result = parse_define_field(
            "DEFINE FIELD a ON t TYPE string ASSERT $value = /x PERMISSIONS FULL / y/ PERMISSIONS FOR select NONE"
        )

        assert result.assertion == "$value = /x PERMISSIONS FULL / y/"
        assert result.permissions == {"select": "NONE"}

    def test_server_output_with_a_space_opened_pattern_keeps_its_clause(self) -> None:
        """Exact SurrealDB 2.7.0 ``INFO FOR TABLE`` output: the ``/`` after ``a+`` ends ``/ a+/``, it opens nothing."""
        result = parse_define_field(
            "DEFINE FIELD a ON r517po TYPE string ASSERT ($value CONTAINS / a+/) "
            "PERMISSIONS FOR select WHERE $auth.x = 1, FOR create, update FULL"
        )

        assert result.assertion == "($value CONTAINS / a+/)"
        assert result.permissions == {"select": "$auth.x = 1"}

    @pytest.mark.xfail(
        strict=True,
        reason="Known limit: a pattern opening with a space is not trusted before a candidate start (ASSERT / +/)",
    )
    def test_a_pattern_opening_with_a_space_is_a_known_limit(self) -> None:
        result = parse_define_field("DEFINE FIELD a ON t TYPE string ASSERT $value = / x PERMISSIONS FULL / y/")

        assert result.assertion == "$value = / x PERMISSIONS FULL / y/"
        assert result.permissions == {}
