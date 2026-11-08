"""Unit tests for #380: a hand-built ``Q`` child is reported by its own field and lookup.

``Q.__init__`` and ``filter()`` split each key with ``parse_filter_key()``, but
a ``Q``'s ``children`` can be set by hand, so the renderer checks each
condition again. It used to rebuild the key (``f"{field}__{lookup}"``) and
split it once more: the child ``("a__b", "exact", 1)`` became the key
``'a__b__exact'`` and was reported as the unknown lookup ``'b__exact'``,
which the caller never wrote. The renderer now checks the field and the
lookup separately, and the message names the one that is wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, Q, SurrealConfigDict
from src.surreal_orm.constants import VALID_LOOKUPS
from src.surreal_orm.paths import FieldPath


class U380Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u380_item")

    id: str | None = None
    name: str = ""
    items: list[dict[str, Any]] = []


_SUPPORTED = f"Supported lookups: {', '.join(sorted(VALID_LOOKUPS))}."


def _hand_built(field: Any, lookup: Any, value: Any = 1) -> Q:
    q = Q(name="x")
    q.children.append((field, lookup, value))
    return q


def _compile_error(q: Q) -> ValueError:
    qs = U380Item.objects().filter(q)
    with pytest.raises(ValueError) as info:
        qs._compile_query()
    return info.value


class TestFieldIsNamed:
    def test_double_underscore_in_field_names_the_field(self) -> None:
        message = str(_compile_error(_hand_built("a__b", "exact")))
        assert message == (
            "Invalid filter field 'a__b': '__' separates a field from its lookup and cannot appear in the field name."
        )
        assert "b__exact" not in message

    @pytest.mark.parametrize("field", ["a b", "name\n", "a-b", "", "x.", "count() AS n"])
    def test_non_field_names_the_field(self, field: str) -> None:
        message = str(_compile_error(_hand_built(field, "exact")))
        assert message == (
            f"Invalid filter field {field!r}: expected a field name or a dotted path of field names "
            "(letters, digits and underscores, each starting with a letter or underscore)."
        )

    @pytest.mark.parametrize("field", [5, None, ("a",)])
    def test_non_string_field_names_the_field(self, field: Any) -> None:
        message = str(_compile_error(_hand_built(field, "exact")))
        assert message.startswith(f"Invalid filter field {field!r}: ")

    def test_field_is_checked_before_the_lookup(self) -> None:
        # Both parts are wrong: the field is reported, as parse_filter_key() does.
        message = str(_compile_error(_hand_built("a b", "bogus")))
        assert message.startswith("Invalid filter field 'a b': ")


class TestLookupIsNamed:
    def test_unknown_lookup_names_the_lookup(self) -> None:
        message = str(_compile_error(_hand_built("name", "bogus")))
        assert message == f"Invalid lookup 'bogus' on filter field 'name'. {_SUPPORTED}"

    @pytest.mark.parametrize("lookup", ["", "gt x", "b__exact", "EXACT"])
    def test_other_unknown_lookups(self, lookup: str) -> None:
        message = str(_compile_error(_hand_built("name", lookup)))
        assert message == f"Invalid lookup {lookup!r} on filter field 'name'. {_SUPPORTED}"

    @pytest.mark.parametrize("lookup", [5, None, ["exact"], {"exact": 1}])
    def test_non_string_lookup_is_a_value_error(self, lookup: Any) -> None:
        # An unhashable lookup must not reach the set membership test (TypeError).
        message = str(_compile_error(_hand_built("name", lookup)))
        assert message == f"Invalid lookup {lookup!r} on filter field 'name': expected a string. {_SUPPORTED}"

    @pytest.mark.parametrize("text", ["exact", "gt", "isnull"])
    def test_non_string_lookup_spelling_a_lookup_is_refused(self, text: str) -> None:
        # The old check re-joined the lookup into a key as text, so an object
        # whose str() is a lookup name passed it and was then rendered as an
        # equality (`name = $_f0`) whatever lookup its text named.
        class Spelled:
            def __str__(self) -> str:
                return text

        lookup = Spelled()
        message = str(_compile_error(_hand_built("name", lookup)))
        assert message == f"Invalid lookup {lookup!r} on filter field 'name': expected a string. {_SUPPORTED}"


class TestEveryRendererReportsTheChild:
    def test_inside_an_element_condition(self) -> None:
        inner = Q(qty=1)
        inner.children.append(("a__b", "exact", 1))
        qs = U380Item.objects().filter(items__any=inner)
        with pytest.raises(ValueError, match=r"^Invalid filter field 'a__b': "):
            qs._compile_query()

    def test_inside_a_field_path_where(self) -> None:
        inner = Q(qty=1)
        inner.children.append(("qty", "bogus", 1))
        qs = U380Item.objects().annotate(x=FieldPath("items").where(inner))
        with pytest.raises(ValueError, match=r"^Invalid lookup 'bogus' on filter field 'qty'\. "):
            qs._compile_query()

    def test_count_query(self) -> None:
        qs = U380Item.objects().filter(_hand_built("a__b", "gt"))
        with pytest.raises(ValueError, match=r"^Invalid filter field 'a__b': "):
            qs._compile_where_clause()


class TestParsedKeysUnchanged:
    def test_filter_kwarg_message_unchanged(self) -> None:
        with pytest.raises(ValueError) as info:
            U380Item.objects().filter(name__bogus=1)
        assert str(info.value) == f"Invalid filter key 'name__bogus': unknown lookup 'bogus'. {_SUPPORTED}"

    def test_q_kwarg_message_unchanged(self) -> None:
        with pytest.raises(ValueError) as info:
            Q(**{"a b": 1})
        assert str(info.value).startswith("Invalid filter key 'a b': 'a b' is not a field name.")

    @pytest.mark.parametrize(
        ("field", "lookup", "value"),
        [("name", "exact", "a"), ("address.city", "icontains", "ly"), ("age", "gte", 3), ("tags", "in", [1, 2])],
    )
    def test_valid_hand_built_child_renders_like_the_kwarg(self, field: str, lookup: str, value: Any) -> None:
        by_hand = Q()
        by_hand.children.append((field, lookup, value))
        hand_sql = U380Item.objects().filter(by_hand)._compile_query()
        kwarg_sql = U380Item.objects().filter(Q(**{f"{field}__{lookup}": value}))._compile_query()
        assert hand_sql == kwarg_sql

    def test_hand_built_field_ending_in_underscore_is_accepted(self) -> None:
        # ``a_`` is a field name. Re-joined as ``a___gt`` it used to split as
        # field ``a`` and lookup ``_gt``, and a valid condition was refused.
        q = Q()
        q.children.append(("a_", "gt", 1))
        sql = U380Item.objects().filter(q)._compile_query()
        assert "WHERE (a_ > $_f0)" in sql or "WHERE a_ > $_f0" in sql
