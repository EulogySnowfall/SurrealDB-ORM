"""Tests for the shared SurrealQL query-string helpers."""

import re

import pytest

from surreal_sdk.utils import find_param_references, substitute_params


class TestSubstituteParams:
    """Tests for substitute_params()."""

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("\\1", id="backreference"),
            pytest.param("\\g<0>", id="group-reference"),
            pytest.param("C:\\temp\\x", id="backslashes"),
            pytest.param('{"nom": "caf\\u00e9"}', id="unicode-escape"),
        ],
    )
    def test_replacement_text_is_inserted_verbatim(self, text: str) -> None:
        """A replacement *string* would be parsed as a mini-pattern.

        ``\\1`` is a backreference, ``\\g<0>`` a group reference, and ``\\u`` an
        unknown escape that raises ``re.error``. The callable form is exempt.
        """
        assert substitute_params("SET a = $a", {"a": text}) == f"SET a = {text}"

    def test_a_bad_escape_would_have_raised_as_a_replacement_string(self) -> None:
        """Pin the failure mode this helper exists to avoid."""
        with pytest.raises(re.error):
            re.sub(r"\$a", '{"nom": "caf\\u00e9"}', "SET a = $a")

    def test_substitution_is_a_single_pass_over_the_original(self) -> None:
        """A ``$b`` inside the text inserted for ``$a`` must not be substituted."""
        result = substitute_params("SET a = $a, b = $b", {"a": "cost $b here", "b": "X"})

        assert result == "SET a = cost $b here, b = X"

    def test_a_longer_reference_is_not_matched_by_a_shorter_key(self) -> None:
        """``\\w+`` is greedy, so ``$_f1`` cannot match inside ``$_f10``."""
        result = substitute_params("x = $_f1 AND y = $_f10", {"_f1": "a", "_f10": "b"})

        assert result == "x = a AND y = b"

    def test_an_unknown_reference_is_left_alone(self) -> None:
        assert substitute_params("SET a = $a", {}) == "SET a = $a"

    def test_a_non_ascii_reference_is_not_a_reference(self) -> None:
        """SurrealDB's identifier lexer is ASCII; the pattern matches it."""
        assert substitute_params("SET a = $pé", {"pé": "X"}) == "SET a = $pé"


class TestFindParamReferences:
    """Tests for find_param_references()."""

    def test_returns_the_referenced_names(self) -> None:
        assert find_param_references("SET a = $a, b = $b_2") == {"a", "b_2"}

    def test_a_name_that_is_not_an_identifier_is_not_referenced(self) -> None:
        """``$my-var`` references ``my``; the caller must not treat it as ``my-var``."""
        assert find_param_references("SET a = $my-var") == {"my"}

    def test_no_references(self) -> None:
        assert find_param_references("SELECT * FROM users") == set()
