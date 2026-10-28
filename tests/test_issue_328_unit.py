"""Unit tests for #328: field names in filters and grouping keys are identifiers.

The field part of a ``filter()`` / ``Q`` key and each ``values()`` name are
written into the generated SurrealQL, so anything that is not a field name must
be refused with ``ValueError`` before a query is compiled. A dotted path into a
nested object stays accepted, one identifier per segment.

The shared identifier pattern was anchored ``^...$`` and tested with
``.match``; ``$`` also matches before a final newline, so ``"name\\n"`` was
accepted as an identifier.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, Count, Q, SurrealConfigDict
from src.surreal_orm.fields import ForeignKey
from src.surreal_orm.utils import SAFE_IDENTIFIER_RE, validate_field_path, validate_identifier


class U328Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u328_item")

    id: str | None = None
    name: str = ""
    age: int = 0
    tags: list[str] = []
    address: dict[str, Any] = {}


class U328Author(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u328_author")

    id: str | None = None
    name: str = ""


class U328Post(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u328_post")

    id: str | None = None
    author: ForeignKey("U328Author") = Field(default=None, alias="author_id")  # type: ignore[valid-type]


BAD_FIELDS = [
    "name x",
    "a-b",
    "a b",
    "name\n",
    "name ",
    " name",
    "",
    "1name",
    "name;",
    "name)",
    "`name`",
    "a..b",
    ".a",
    "a.",
    "a.b c",
    "a.1b",
    "tags[0]",
    "items.*.price",
]


# ── The identifier check itself ──────────────────────────────────────────


class TestIdentifier:
    @pytest.mark.parametrize("name", ["name\n", "name \n", "name\r\n"])
    def test_validate_identifier_rejects_trailing_newline(self, name: str) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            validate_identifier(name)

    @pytest.mark.parametrize("name", ["name\n", "name "])
    def test_pattern_rejects_trailing_whitespace_with_match_too(self, name: str) -> None:
        # Call sites outside this package may still use .match(); the pattern
        # itself must not accept a name followed by a newline.
        assert SAFE_IDENTIFIER_RE.match(name) is None
        assert SAFE_IDENTIFIER_RE.fullmatch(name) is None

    @pytest.mark.parametrize("name", ["name", "_x", "a1", "A_B_9"])
    def test_valid_identifiers_still_pass(self, name: str) -> None:
        validate_identifier(name)

    @pytest.mark.parametrize("path", ["name", "address.city", "a.b.c", "_a._b"])
    def test_field_path_accepts_dotted_identifiers(self, path: str) -> None:
        validate_field_path(path)

    @pytest.mark.parametrize("path", BAD_FIELDS)
    def test_field_path_rejects_anything_else(self, path: str) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            validate_field_path(path)


# ── Direct identifier checks in the model layer ──────────────────────────


class TestModelChecks:
    @pytest.mark.parametrize("method", ["atomic_append", "atomic_remove", "atomic_set_add"])
    async def test_atomic_ops_reject_trailing_newline(self, method: str) -> None:
        with pytest.raises(ValueError, match="Invalid field name"):
            await getattr(U328Item, method)("u328_item:a", "tags\n", "x")

    def test_set_clause_rejects_trailing_newline(self) -> None:
        with pytest.raises(ValueError, match="Invalid field name"):
            U328Item._build_set_clause({"name\n": "x"})

    async def test_relate_rejects_trailing_newline(self) -> None:
        item = U328Item(id="a")
        with pytest.raises(ValueError, match="Invalid relation name"):
            await item.relate("likes\n", U328Item(id="b"))

    async def test_bulk_update_rejects_trailing_newline(self) -> None:
        with pytest.raises(ValueError, match="Invalid field name"):
            await U328Item.objects().filter(age=1).bulk_update({"name\n": "x"})


# ── Table-name checks in the SDK ─────────────────────────────────────────


class TestSdkTableNames:
    @pytest.mark.parametrize("table", ["users\n", "users "])
    def test_streams_reject_trailing_whitespace(self, table: str) -> None:
        from surreal_sdk.streaming.change_feed import ChangeFeedStream
        from surreal_sdk.streaming.live_select import LiveSelectStream

        with pytest.raises(ValueError, match="Invalid table name"):
            ChangeFeedStream(MagicMock(), table)
        with pytest.raises(ValueError, match="Invalid table name"):
            LiveSelectStream(MagicMock(), table)

    def test_streams_accept_plain_table(self) -> None:
        from surreal_sdk.streaming.change_feed import ChangeFeedStream
        from surreal_sdk.streaming.live_select import LiveSelectStream

        assert ChangeFeedStream(MagicMock(), "users").table == "users"
        assert LiveSelectStream(MagicMock(), "users").table == "users"


# ── filter(), Q and values() ─────────────────────────────────────────────


class TestFilterKeys:
    @pytest.mark.parametrize("key", ["name x", "a b", "a-b", "name\n", "string::len(name)", "name__gt x"])
    def test_filter_rejects_non_identifier_key(self, key: str) -> None:
        with pytest.raises(ValueError) as exc:
            U328Item.objects().filter(**{key: 1})
        assert repr(key) in str(exc.value)

    @pytest.mark.parametrize("field", BAD_FIELDS)
    def test_filter_rejects_bad_field_with_lookup(self, field: str) -> None:
        with pytest.raises(ValueError):
            U328Item.objects().filter(**{f"{field}__gte": 1})

    @pytest.mark.parametrize("key", ["a-b", "a b", "name\n", "x.y z__exact"])
    def test_q_rejects_non_identifier_key(self, key: str) -> None:
        with pytest.raises(ValueError) as exc:
            Q(**{key: 1})
        assert repr(key) in str(exc.value)

    def test_negated_q_rejects_non_identifier_key(self) -> None:
        # ``~Q(...)`` is how a filter is excluded; the key is checked when the
        # Q is built, before it can be combined or negated.
        with pytest.raises(ValueError):
            U328Item.objects().filter(~Q(**{"a b": 1}))

    def test_q_children_built_by_hand_are_checked_when_compiled(self) -> None:
        q = Q(name="x")
        q.children.append(("a b", "exact", 1))
        qs = U328Item.objects().filter(q)
        with pytest.raises(ValueError):
            qs._compile_query()

    @pytest.mark.parametrize("lookup", ["iexact", "gt x", "foo", ""])
    def test_unknown_lookup_raises(self, lookup: str) -> None:
        with pytest.raises(ValueError, match="lookup"):
            U328Item.objects().filter(**{f"name__{lookup}": 1})
        with pytest.raises(ValueError, match="lookup"):
            Q(**{f"name__{lookup}": 1})

    @pytest.mark.parametrize("name", ["a b", "name\n", "count() AS n", "status, secret", ""])
    def test_values_rejects_non_identifier(self, name: str) -> None:
        with pytest.raises(ValueError) as exc:
            U328Item.objects().values("name", name)
        assert repr(name) in str(exc.value)

    def test_values_accepts_dotted_path(self) -> None:
        qs = U328Item.objects().values("address.city").annotate(n=Count())
        assert "GROUP BY address.city" in qs._compile_annotate_query()


# ── Valid forms render exactly as before ─────────────────────────────────


class TestValidFormsUnchanged:
    def test_plain_field(self) -> None:
        qs = U328Item.objects().filter(name="a")
        assert qs._compile_query() == "SELECT * FROM u328_item WHERE name = $_f0;"

    def test_operator_lookup(self) -> None:
        qs = U328Item.objects().filter(age__gte=18)
        assert qs._compile_query() == "SELECT * FROM u328_item WHERE age >= $_f0;"

    def test_collection_lookup(self) -> None:
        qs = U328Item.objects().filter(tags__containsall=["a", "b"])
        assert qs._compile_query() == "SELECT * FROM u328_item WHERE tags CONTAINSALL $_f0;"

    def test_function_lookup(self) -> None:
        qs = U328Item.objects().filter(name__istartswith="Al")
        assert qs._compile_query() == "SELECT * FROM u328_item WHERE string::starts_with(string::lowercase(name), $_f0);"

    def test_isnull(self) -> None:
        qs = U328Item.objects().filter(name__isnull=True)
        assert qs._compile_query() == "SELECT * FROM u328_item WHERE name IS NULL;"

    def test_dotted_path(self) -> None:
        qs = U328Item.objects().filter(**{"address.city": "Paris", "address.zip__startswith": "75"})
        assert qs._compile_query() == (
            "SELECT * FROM u328_item WHERE address.city = $_f0 AND string::starts_with(address.zip, $_f1);"
        )

    def test_aliased_foreign_key_column(self) -> None:
        qs = U328Post.objects().filter(author_id="u328_author:alice")
        assert qs._compile_query() == "SELECT * FROM u328_post WHERE author_id = $_f0;"
        assert str(qs._variables["_f0"]) == "u328_author:alice"

    def test_q_combinations(self) -> None:
        qs = U328Item.objects().filter((Q(name="a") | Q(age__lt=3)) & ~Q(tags__contains="x"), age__gt=1)
        assert qs._compile_query() == (
            "SELECT * FROM u328_item WHERE age > $_f0 AND ((name = $_f1 OR age < $_f2) AND NOT (tags CONTAINS $_f3));"
        )

    def test_values_group_by(self) -> None:
        qs = U328Item.objects().values("name", "age").annotate(n=Count())
        assert qs._compile_annotate_query() == "SELECT name, age, count() AS n FROM u328_item GROUP BY name, age;"


class U328Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u328_person")

    id: str | None = None
    name: str = ""
    age: int = 0


def _relation_qs() -> Any:
    from src.surreal_orm.fields.relation import RelationInfo
    from src.surreal_orm.relations import RelationManager

    person = U328Person(id="alice", name="Alice")
    info = RelationInfo(to_model="U328Person", relation_type="relation", edge_table="u328_follows")
    return RelationManager(person, info, "following")


class TestRelationFilterKeys:
    """``instance.<relation>.filter()`` writes its keys into the traversal query too."""

    @pytest.mark.parametrize("key", ["a b", "true OR name", "name\n", "a-b", "name x__gt"])
    def test_non_identifier_key_raises(self, key: str) -> None:
        with pytest.raises(ValueError, match=re.escape(repr(key))):
            _relation_qs().filter(**{key: 1})

    @pytest.mark.parametrize("key", ["name__foo", "name__b__gt"])
    def test_unknown_lookup_raises(self, key: str) -> None:
        with pytest.raises(ValueError, match=re.escape(repr(key))):
            _relation_qs().filter(**{key: 1})

    def test_lookup_not_rendered_for_relations_raises(self) -> None:
        # A lookup the ORM knows but the traversal renderer does not handle
        # used to fall back to ``=`` silently.
        with pytest.raises(ValueError, match="iregex"):
            _relation_qs().filter(name__iregex="^a")

    def test_valid_keys_render_as_before(self) -> None:
        qs = _relation_qs().filter(name="Bob", age__gte=18, deleted__isnull=True, **{"address.city": "x"})
        query, variables = qs._build_traversal_query()
        assert query == (
            "SELECT * FROM u328_person:alice->u328_follows->U328Person WHERE "
            "name = $filter_0 AND age >= $filter_1 AND deleted IS NULL AND address.city = $filter_3;"
        )
        assert variables == {"filter_0": "Bob", "filter_1": 18, "filter_3": "x"}
