"""Unit tests for #377: field names in filters and grouping keys are identifiers (v2).

The field part of a ``filter()`` / ``Q`` key and each ``values()`` name are
written into the generated SurrealQL, so anything that is not a field name must
be refused with ``ValueError`` before a query is compiled. A dotted path into a
nested object stays accepted, one identifier per segment. An unknown lookup used
to be rendered as ``=``; it raises now, in relation filters too.

The shared identifier pattern was anchored ``^...$`` and tested with
``.match``; ``$`` also matches before a final newline, so ``"name\\n"`` was
accepted as an identifier.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, Count, Q, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.fields import ForeignKey
from src.surreal_orm.model_base import to_record_id
from src.surreal_orm.utils import (
    SAFE_IDENTIFIER_RE,
    format_thing,
    needs_id_escaping,
    parse_filter_key,
    validate_field_path,
    validate_identifier,
)
from surreal_sdk.protocol.cbor import RecordId


class U377Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u377_item")

    id: str | None = None
    name: str = ""
    age: int = 0
    tags: list[str] = []
    address: dict[str, Any] = {}


class U377Author(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u377_author")

    id: str | None = None
    name: str = ""


class U377Post(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u377_post")

    id: str | None = None
    author: ForeignKey("U377Author") = Field(default=None, alias="author_id")  # type: ignore[valid-type]


class U377Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u377_person")

    id: str | None = None
    name: str = ""
    age: int = 0


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


def _mock_client() -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.all_records = []
    client.query = AsyncMock(return_value=result)
    return client


async def _annotate_sql(qs: Any) -> str:
    """Run ``_execute_annotate()`` against a mock client and return the query sent."""
    client = _mock_client()
    with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
        await qs._execute_annotate()
    sql: str = client.query.call_args.args[0]
    return sql


# ── The identifier check itself ──────────────────────────────────────────


class TestIdentifier:
    @pytest.mark.parametrize("name", ["name\n", "name \n", "name\r\n"])
    def test_validate_identifier_rejects_trailing_newline(self, name: str) -> None:
        with pytest.raises(ValueError, match="Invalid"):
            validate_identifier(name)

    @pytest.mark.parametrize("name", ["name\n", "name "])
    def test_pattern_rejects_trailing_whitespace_with_match_too(self, name: str) -> None:
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

    def test_parse_filter_key_splits_on_first_double_underscore(self) -> None:
        assert parse_filter_key("age__gte") == ("age", "gte")
        assert parse_filter_key("status") == ("status", "exact")
        assert parse_filter_key("address.city__startswith") == ("address.city", "startswith")


# ── Direct identifier checks in the model layer ──────────────────────────


class TestModelChecks:
    @pytest.mark.parametrize("method", ["atomic_append", "atomic_remove", "atomic_set_add"])
    async def test_atomic_ops_reject_trailing_newline(self, method: str) -> None:
        with pytest.raises(ValueError, match="Invalid field name"):
            await getattr(U377Item, method)("u377_item:a", "tags\n", "x")

    def test_set_clause_rejects_trailing_newline(self) -> None:
        with pytest.raises(ValueError, match="Invalid field name"):
            U377Item._build_set_clause({"name\n": "x"})

    async def test_server_values_reject_trailing_newline(self) -> None:
        from src.surreal_orm import SurrealFunc

        item = U377Item(id="a")
        with pytest.raises(ValueError, match="server_values"):
            await item.save(server_values={"name\n": SurrealFunc("time::now()")})

    async def test_relate_rejects_trailing_newline(self) -> None:
        item = U377Item(id="a")
        with pytest.raises(ValueError, match="Invalid relation name"):
            await item.relate("likes\n", U377Item(id="b"))

    async def test_remove_relation_rejects_trailing_newline(self) -> None:
        item = U377Item(id="a")
        with pytest.raises(ValueError, match="Invalid relation name"):
            await item.remove_relation("likes\n", U377Item(id="b"))

    async def test_remove_all_relations_rejects_trailing_newline(self) -> None:
        item = U377Item(id="a")
        with pytest.raises(ValueError, match="Invalid relation name"):
            await item.remove_all_relations(["likes", "follows\n"])

    async def test_get_related_rejects_trailing_newline(self) -> None:
        item = U377Item(id="a")
        with pytest.raises(ValueError, match="Invalid relation name"):
            await item.get_related("likes\n")

    async def test_bulk_update_rejects_trailing_newline(self) -> None:
        with pytest.raises(ValueError, match="Invalid field name"):
            await U377Item.objects().filter(age=1).bulk_update({"name\n": "x"})


# ── Record-ID rendering (same full-match rule) ───────────────────────────


class TestRecordIdRendering:
    def test_needs_id_escaping_trailing_newline(self) -> None:
        assert needs_id_escaping("abc\n") is True

    def test_needs_id_escaping_plain_id_unchanged(self) -> None:
        assert needs_id_escaping("abc") is False

    def test_format_thing_backticks_trailing_newline(self) -> None:
        assert format_thing("t", "abc\n") == "t:`abc\n`"

    def test_format_thing_plain_id_unchanged(self) -> None:
        assert format_thing("t", "abc") == "t:abc"

    def test_to_record_id_rejects_table_with_trailing_newline(self) -> None:
        assert to_record_id("t\n:abc") == "t\n:abc"

    def test_to_record_id_plain_table_unchanged(self) -> None:
        result = to_record_id("t:abc")
        assert isinstance(result, RecordId)
        assert (result.table, result.id) == ("t", "abc")


# ── Table-name checks in the SDK ─────────────────────────────────────────


class TestSdkTableNames:
    @pytest.mark.parametrize("table", ["users\n", "users "])
    def test_streams_reject_trailing_whitespace(self, table: str) -> None:
        from surreal_sdk.streaming.change_feed import ChangeFeedStream
        from surreal_sdk.streaming.live_query import LiveQuery
        from surreal_sdk.streaming.live_select import LiveSelectStream

        with pytest.raises(ValueError, match="Invalid table name"):
            ChangeFeedStream(MagicMock(), table)
        with pytest.raises(ValueError, match="Invalid table name"):
            LiveSelectStream(MagicMock(), table)
        with pytest.raises(ValueError, match="Invalid table name"):
            LiveQuery(MagicMock(), table)

    @pytest.mark.parametrize("table", ["users\n", "users "])
    async def test_websocket_live_rejects_trailing_whitespace(self, table: str) -> None:
        from surreal_sdk import LiveSubscriptionParams
        from surreal_sdk.connection.websocket import WebSocketConnection

        conn = MagicMock()
        with pytest.raises(ValueError, match="Invalid table name"):
            await WebSocketConnection.live(conn, table, AsyncMock())
        params = MagicMock(spec=LiveSubscriptionParams)
        params.table = table
        with pytest.raises(ValueError, match="Invalid table name"):
            await WebSocketConnection._resubscribe_one(conn, params)

    def test_streams_accept_plain_table(self) -> None:
        from surreal_sdk.streaming.change_feed import ChangeFeedStream
        from surreal_sdk.streaming.live_query import LiveQuery
        from surreal_sdk.streaming.live_select import LiveSelectStream

        assert ChangeFeedStream(MagicMock(), "users").table == "users"
        assert LiveSelectStream(MagicMock(), "users").table == "users"
        assert LiveQuery(MagicMock(), "users").table == "users"


# ── filter(), Q and values() ─────────────────────────────────────────────


class TestFilterKeys:
    @pytest.mark.parametrize("key", ["name x", "a b", "a-b", "name\n", "string::len(name)", "name__gt x"])
    def test_filter_rejects_non_identifier_key(self, key: str) -> None:
        with pytest.raises(ValueError) as exc:
            U377Item.objects().filter(**{key: 1})
        assert repr(key) in str(exc.value)

    @pytest.mark.parametrize("field", BAD_FIELDS)
    def test_filter_rejects_bad_field_with_lookup(self, field: str) -> None:
        with pytest.raises(ValueError):
            U377Item.objects().filter(**{f"{field}__gte": 1})

    @pytest.mark.parametrize("key", ["a-b", "a b", "name\n", "x.y z__exact"])
    def test_q_rejects_non_identifier_key(self, key: str) -> None:
        with pytest.raises(ValueError) as exc:
            Q(**{key: 1})
        assert repr(key) in str(exc.value)

    def test_negated_q_rejects_non_identifier_key(self) -> None:
        with pytest.raises(ValueError, match=re.escape(repr("a b"))):
            U377Item.objects().filter(~Q(**{"a b": 1}))

    def test_q_children_built_by_hand_are_checked_when_compiled(self) -> None:
        q = Q(name="x")
        q.children.append(("a b", "exact", 1))
        qs = U377Item.objects().filter(q)
        with pytest.raises(ValueError):
            qs._compile_query()

    @pytest.mark.parametrize("lookup", ["iexact", "gt x", "foo", ""])
    def test_unknown_lookup_raises(self, lookup: str) -> None:
        key = f"name__{lookup}"
        with pytest.raises(ValueError, match="lookup") as exc:
            U377Item.objects().filter(**{key: 1})
        assert repr(key) in str(exc.value)
        with pytest.raises(ValueError, match="lookup"):
            Q(**{key: 1})

    async def test_unknown_lookup_sends_no_query(self) -> None:
        client = _mock_client()
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
            with pytest.raises(ValueError):
                await U377Item.objects().filter(name__iexact="ALPHA").exec()
        client.query.assert_not_called()

    @pytest.mark.parametrize("name", ["a b", "name\n", "count() AS n", "status, secret", ""])
    def test_values_rejects_non_identifier(self, name: str) -> None:
        with pytest.raises(ValueError) as exc:
            U377Item.objects().values("name", name)
        assert repr(name) in str(exc.value)

    async def test_values_accepts_dotted_path(self) -> None:
        qs = U377Item.objects().values("address.city").annotate(n=Count())
        assert "GROUP BY address.city" in await _annotate_sql(qs)


# ── Valid forms render exactly as before ─────────────────────────────────


class TestValidFormsUnchanged:
    def test_plain_field(self) -> None:
        qs = U377Item.objects().filter(name="a")
        assert qs._compile_query() == "SELECT * FROM u377_item WHERE name = $_f0;"

    def test_operator_lookup(self) -> None:
        qs = U377Item.objects().filter(age__gte=18)
        assert qs._compile_query() == "SELECT * FROM u377_item WHERE age >= $_f0;"

    def test_collection_lookup(self) -> None:
        qs = U377Item.objects().filter(tags__containsall=["a", "b"])
        assert qs._compile_query() == "SELECT * FROM u377_item WHERE tags CONTAINSALL $_f0;"

    def test_isnull(self) -> None:
        qs = U377Item.objects().filter(name__isnull=True)
        assert qs._compile_query() == "SELECT * FROM u377_item WHERE name IS NULL;"

    def test_dotted_path(self) -> None:
        qs = U377Item.objects().filter(**{"address.city": "Paris", "address.zip__startswith": "75"})
        assert qs._compile_query() == (
            "SELECT * FROM u377_item WHERE address.city = $_f0 AND string::starts_with(address.zip, $_f1);"
        )

    def test_aliased_foreign_key_column(self) -> None:
        qs = U377Post.objects().filter(author_id="u377_author:alice")
        assert qs._compile_query() == "SELECT * FROM u377_post WHERE author_id = $_f0;"
        assert str(qs._variables["_f0"]) == "u377_author:alice"

    def test_q_combinations(self) -> None:
        qs = U377Item.objects().filter((Q(name="a") | Q(age__lt=3)) & ~Q(tags__contains="x"), age__gt=1)
        assert qs._compile_query() == (
            "SELECT * FROM u377_item WHERE age > $_f0 AND ((name = $_f1 OR age < $_f2) AND NOT (tags CONTAINS $_f3));"
        )

    async def test_values_group_by(self) -> None:
        qs = U377Item.objects().values("name", "age").annotate(n=Count())
        assert await _annotate_sql(qs) == "SELECT name, age, count() AS n FROM u377_item GROUP BY name, age;"


# ── Relation filters ─────────────────────────────────────────────────────


def _relation_qs() -> Any:
    from src.surreal_orm.fields.relation import RelationInfo
    from src.surreal_orm.relations import RelationManager

    person = U377Person(id="alice", name="Alice")
    info = RelationInfo(to_model="U377Person", relation_type="relation", edge_table="u377_follows")
    return RelationManager(person, info, "following")


class TestRelationFilterKeys:
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

    def test_get_filter_clause_rejects_unrendered_lookup(self) -> None:
        from src.surreal_orm.relations import RelationQuerySet

        with pytest.raises(ValueError, match="not_in"):
            RelationQuerySet._get_filter_clause(MagicMock(), "name", "not_in", "v", [1])

    def test_valid_keys_render_as_before(self) -> None:
        qs = _relation_qs().filter(name="Bob", age__gte=18, deleted__isnull=True, **{"address.city": "x"})
        query, variables = qs._build_traversal_query()
        assert query == (
            "SELECT * FROM u377_person:alice->u377_follows->U377Person WHERE "
            "name = $filter_0 AND age >= $filter_1 AND deleted IS NULL AND address.city = $filter_3;"
        )
        assert variables == {"filter_0": "Bob", "filter_1": 18, "filter_3": "x"}
