"""
Unit tests for ForeignKey values being sent as record links.

A :func:`ForeignKey` holds a ``"table:id"`` string in Python, but a
``record<...>`` column only ever matches a real record value.  Historically the
string was passed straight through, which failed in two different ways:

- A **write** was rejected by SurrealDB (``Expected record<users> but found
  'users:alice'``) — loud, and therefore easy to spot.
- A **filter** silently matched nothing, because a bound string never equates
  to a record — an empty result rather than an error.

These tests pin down the fixed behavior on both paths, and the three ways a
reference may be given: the related instance, a full ``"table:id"`` string, or
a bare ID resolved against the target model's table.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional, get_type_hints
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field
from pydantic_core import ValidationError

from src.surreal_orm import signals as model_signals
from src.surreal_orm.fields.references import ReferencesField, _ReferencesMarker
from src.surreal_orm.fields.relation import (
    ForeignKey,
    ManyToMany,
    Relation,
    _ForeignKeyMarker,
    _ManyToManyMarker,
    _RelationMarker,
)
from src.surreal_orm.migrations.introspector import ModelIntrospector, _get_relation_marker
from src.surreal_orm.migrations.operations import AddField
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    record_link_to_str,
    to_record_id,
)
from src.surreal_orm.q import Q
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.protocol.rpc import SurrealJSONEncoder

# ==================== Test Models ====================
# Names are prefixed because the model registry is global: a target is resolved
# by model name, so a plain "User" would collide with other test modules.


class FkUser(BaseSurrealModel):
    """Target of the foreign key — its table name differs from the class name."""

    model_config = SurrealConfigDict(table_name="fk_users")

    id: str | None = None
    name: str = Field(default="")


class FkPost(BaseSurrealModel):
    """Model with a ForeignKey field."""

    model_config = SurrealConfigDict(table_name="fk_posts")

    id: str | None = None
    title: str = Field(default="")
    author: ForeignKey("FkUser")  # type: ignore[valid-type]


class FkComment(BaseSurrealModel):
    """Model whose ForeignKey is stored under a different column name."""

    model_config = SurrealConfigDict(table_name="fk_comments")

    id: str | None = None
    body: str = Field(default="")
    post: ForeignKey("FkPost") = Field(default=None, alias="post_id")  # type: ignore[valid-type]


class FkOrphan(BaseSurrealModel):
    """Model whose ForeignKey points at a model that was never defined."""

    model_config = SurrealConfigDict(table_name="fk_orphans")

    id: str | None = None
    ref: ForeignKey("NeverDefined")  # type: ignore[valid-type]


class FkTag(BaseSurrealModel):
    """Model without any relation field."""

    model_config = SurrealConfigDict(table_name="fk_tags")

    id: str | None = None
    label: str = Field(default="")


class FkGroup(BaseSurrealModel):
    """Target of a many-to-many relation."""

    model_config = SurrealConfigDict(table_name="fk_groups")

    id: str | None = None


class FkMember(BaseSurrealModel):
    """Model with a ManyToMany field."""

    model_config = SurrealConfigDict(table_name="fk_members")

    id: str | None = None
    groups: ManyToMany("FkGroup") = Field(default_factory=list)  # type: ignore[valid-type]


# ==================== to_record_id() ====================


class TestToRecordId:
    """Tests for the string → RecordId conversion helper."""

    def test_full_record_id_is_converted(self) -> None:
        assert to_record_id("users:alice") == RecordId(table="users", id="alice")

    def test_escaped_id_is_unescaped(self) -> None:
        assert to_record_id("users:`7abc`") == RecordId(table="users", id="7abc")

    def test_bare_id_is_unchanged(self) -> None:
        """Without a target table this helper cannot qualify a bare ID."""
        assert to_record_id("alice") == "alice"

    def test_none_is_unchanged(self) -> None:
        assert to_record_id(None) is None

    def test_non_string_is_unchanged(self) -> None:
        assert to_record_id(42) == 42

    def test_record_id_is_unchanged(self) -> None:
        record = RecordId(table="users", id="alice")
        assert to_record_id(record) is record

    def test_variable_reference_is_unchanged(self) -> None:
        assert to_record_id("$author") == "$author"

    def test_non_identifier_prefix_is_unchanged(self) -> None:
        """A timestamp contains ':' but '2026-08-23T10' is not a table name."""
        assert to_record_id("2026-08-23T10:00:00Z") == "2026-08-23T10:00:00Z"


# ==================== get_foreign_key_targets() ====================


class TestGetForeignKeyTargets:
    """Tests for foreign-key discovery and target resolution."""

    def test_resolves_target_table(self) -> None:
        """The target is the model's table name, not its class name."""
        assert FkPost.get_foreign_key_targets() == {"author": "fk_users"}

    def test_unknown_target_resolves_to_none(self) -> None:
        assert FkOrphan.get_foreign_key_targets() == {"ref": None}

    def test_model_without_relations(self) -> None:
        assert FkTag.get_foreign_key_targets() == {}


# ==================== Assignment ====================


class TestRecordIdProperty:
    """Tests for the reserved ``record_id`` accessor (the Django ``pk`` analog)."""

    def test_saved_instance(self) -> None:
        assert FkUser(id="alice").record_id == RecordId(table="fk_users", id="alice")

    def test_unsaved_instance(self) -> None:
        assert FkUser(name="Alice").record_id is None

    def test_str_form(self) -> None:
        """str() of the property is the "table:id" record link."""
        assert str(FkUser(id="alice").record_id) == "fk_users:alice"

    def test_not_serialized(self) -> None:
        """A property, not a field — never leaks into dumps or saves."""
        assert "record_id" not in FkUser(id="alice").model_dump()

    def test_usable_as_bound_variable(self) -> None:
        """The intended use: binding against a record<> column."""
        author = FkUser(id="alice")
        qs = FkPost.objects().filter(author="$a").variables(a=author.record_id)
        query = qs._compile_query()
        assert "author = $a" in query
        assert qs._variables["a"] == RecordId(table="fk_users", id="alice")


class TestForeignKeyAssignment:
    """Tests that a foreign-key field accepts the related object."""

    def test_instance_is_stored_as_string(self) -> None:
        author = FkUser(id="alice", name="Alice")
        post = FkPost(title="Hello", author=author)
        assert post.author == "fk_users:alice"

    def test_record_id_is_stored_as_string(self) -> None:
        post = FkPost(title="Hello", author=RecordId(table="fk_users", id="alice"))
        assert post.author == "fk_users:alice"

    def test_string_is_left_alone(self) -> None:
        post = FkPost(title="Hello", author="fk_users:alice")
        assert post.author == "fk_users:alice"

    def test_assignment_after_construction(self) -> None:
        post = FkPost(title="Hello", author="fk_users:alice")
        post.author = FkUser(id="bob", name="Bob")  # type: ignore[assignment]
        assert post.author == "fk_users:bob"

    def test_unsaved_instance_is_rejected(self) -> None:
        """An instance with no ID has nothing to reference yet."""
        with pytest.raises(ValidationError):
            FkPost(title="Hello", author=FkUser(name="Alice"))

    def test_many_to_many_accepts_mixed_references(self) -> None:
        """A ManyToMany list takes instances, RecordIds, and strings alike."""
        member = FkMember(
            groups=[
                FkGroup(id="g1"),
                RecordId(table="fk_groups", id="g2"),
                "fk_groups:g3",
            ],
        )
        assert member.groups == ["fk_groups:g1", "fk_groups:g2", "fk_groups:g3"]


# ==================== Write path ====================


class TestCoerceForeignKeysOnWrite:
    """Tests that foreign keys leave the ORM as record links."""

    def test_string_is_wrapped(self) -> None:
        post = FkPost(title="Hello", author="fk_users:alice")
        data = post._coerce_foreign_keys({"title": "Hello", "author": "fk_users:alice"})
        assert data["author"] == RecordId(table="fk_users", id="alice")
        assert data["title"] == "Hello"

    def test_bare_id_is_qualified_with_target_table(self) -> None:
        post = FkPost(title="Hello", author="alice")
        data = post._coerce_foreign_keys({"author": "alice"})
        assert data["author"] == RecordId(table="fk_users", id="alice")

    def test_bare_id_without_known_target_is_unchanged(self) -> None:
        orphan = FkOrphan(ref="alice")
        data = orphan._coerce_foreign_keys({"ref": "alice"})
        assert data["ref"] == "alice"

    def test_none_is_preserved(self) -> None:
        post = FkPost(title="Hello", author=None)
        data = post._coerce_foreign_keys({"author": None})
        assert data["author"] is None

    def test_non_relation_field_is_untouched(self) -> None:
        """Only declared foreign keys are converted, colon or not."""
        tag = FkTag(label="data:image/png;base64,AAA")
        data = tag._coerce_foreign_keys({"label": "data:image/png;base64,AAA"})
        assert data["label"] == "data:image/png;base64,AAA"

    def test_aliased_field_is_resolved(self) -> None:
        comment = FkComment(body="Nice", post_id="fk_posts:1")
        data = comment._coerce_foreign_keys({"post_id": "fk_posts:1"})
        assert data["post_id"] == RecordId(table="fk_posts", id="1")

    def test_input_dict_is_not_mutated(self) -> None:
        post = FkPost(title="Hello", author="fk_users:alice")
        original = {"author": "fk_users:alice"}
        post._coerce_foreign_keys(original)
        assert original["author"] == "fk_users:alice"

    @pytest.mark.asyncio
    async def test_save_sends_record_id(self) -> None:
        """save() on a new record must not send the foreign key as a string."""
        mock_client = AsyncMock()
        mock_client.create = AsyncMock(
            return_value=MagicMock(exists=True, record={"id": "fk_posts:1", "title": "Hello"}),
        )

        post = FkPost(title="Hello", author="fk_users:alice")
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await post.save()

        _table, data = mock_client.create.call_args[0]
        assert data["author"] == RecordId(table="fk_users", id="alice")

    @pytest.mark.asyncio
    async def test_merge_accepts_an_instance(self) -> None:
        """merge() takes the related object and writes a record link."""
        mock_client = AsyncMock()
        mock_client.merge = AsyncMock(return_value=MagicMock(is_empty=False))

        post = FkPost(id="1", title="Hello", author="fk_users:alice")
        post._db_persisted = True
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await post.merge(refresh=False, author=FkUser(id="bob", name="Bob"))

        _thing, data = mock_client.merge.call_args[0]
        assert data["author"] == RecordId(table="fk_users", id="bob")


# ==================== Filter path ====================


class TestCoerceForeignKeysInFilters:
    """Tests that foreign-key filters bind a record link, not a string."""

    def test_exact_filter_binds_record_id(self) -> None:
        qs = FkPost.objects().filter(author="fk_users:alice")
        qs._compile_query()
        assert qs._variables["_f0"] == RecordId(table="fk_users", id="alice")

    def test_bare_id_filter_binds_record_id(self) -> None:
        qs = FkPost.objects().filter(author="alice")
        qs._compile_query()
        assert qs._variables["_f0"] == RecordId(table="fk_users", id="alice")

    def test_instance_filter_binds_record_id(self) -> None:
        qs = FkPost.objects().filter(author=FkUser(id="alice", name="Alice"))
        qs._compile_query()
        assert qs._variables["_f0"] == RecordId(table="fk_users", id="alice")

    def test_record_id_filter_is_passed_through(self) -> None:
        qs = FkPost.objects().filter(author=RecordId(table="fk_users", id="alice"))
        qs._compile_query()
        assert qs._variables["_f0"] == RecordId(table="fk_users", id="alice")

    def test_in_filter_binds_record_ids(self) -> None:
        qs = FkPost.objects().filter(author__in=["fk_users:alice", "bob"])
        qs._compile_query()
        assert qs._variables["_f0"] == [
            RecordId(table="fk_users", id="alice"),
            RecordId(table="fk_users", id="bob"),
        ]

    def test_in_filter_accepts_mixed_forms(self) -> None:
        """All four reference forms can be mixed in one collection."""
        qs = FkPost.objects().filter(
            author__in=[
                FkUser(id="alice", name="Alice"),
                "fk_users:bob",
                "carol",
                RecordId(table="fk_users", id="dave"),
            ],
        )
        qs._compile_query()
        assert qs._variables["_f0"] == [
            RecordId(table="fk_users", id="alice"),
            RecordId(table="fk_users", id="bob"),
            RecordId(table="fk_users", id="carol"),
            RecordId(table="fk_users", id="dave"),
        ]

    def test_in_filter_accepts_tuple(self) -> None:
        qs = FkPost.objects().filter(author__in=("alice", "bob"))
        qs._compile_query()
        assert list(qs._variables["_f0"]) == [
            RecordId(table="fk_users", id="alice"),
            RecordId(table="fk_users", id="bob"),
        ]

    def test_not_in_filter_binds_record_ids(self) -> None:
        qs = FkPost.objects().filter(author__not_in=["fk_users:spam"])
        query = qs._compile_query()
        assert "author NOT IN $_f0" in query
        assert qs._variables["_f0"] == [RecordId(table="fk_users", id="spam")]

    def test_q_object_filter_binds_record_id(self) -> None:
        qs = FkPost.objects().filter(Q(author="fk_users:alice") | Q(author="fk_users:bob"))
        qs._compile_query()
        assert qs._variables["_f0"] == RecordId(table="fk_users", id="alice")
        assert qs._variables["_f1"] == RecordId(table="fk_users", id="bob")

    def test_negated_q_filter_binds_record_id(self) -> None:
        qs = FkPost.objects().filter(~Q(author="fk_users:banned"))
        query = qs._compile_query()
        assert "NOT (author = $_f0)" in query
        assert qs._variables["_f0"] == RecordId(table="fk_users", id="banned")

    def test_mixed_fk_and_plain_fields(self) -> None:
        """Only the foreign key is converted; other fields bind raw values."""
        qs = FkPost.objects().filter(author="alice", title="Hello")
        qs._compile_query()
        assert qs._variables["_f0"] == RecordId(table="fk_users", id="alice")
        assert qs._variables["_f1"] == "Hello"

    def test_string_lookups_are_untouched(self) -> None:
        """A record link makes no sense inside string::starts_with()."""
        qs = FkPost.objects().filter(author__startswith="fk_users:a")
        qs._compile_query()
        assert qs._variables["_f0"] == "fk_users:a"

    def test_non_relation_field_is_untouched(self) -> None:
        qs = FkPost.objects().filter(title="a:b")
        qs._compile_query()
        assert qs._variables["_f0"] == "a:b"

    def test_isnull_filter_is_untouched(self) -> None:
        qs = FkPost.objects().filter(author__isnull=True)
        query = qs._compile_query()
        assert "author IS NULL" in query

    def test_variable_reference_is_untouched(self) -> None:
        qs = FkPost.objects().filter(author="$author").variables(author="fk_users:alice")
        query = qs._compile_query()
        assert "author = $author" in query

    @pytest.mark.asyncio
    async def test_bulk_update_binds_record_id(self) -> None:
        """bulk_update() writes foreign keys as record links too."""
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(return_value=MagicMock(all_records=[{}]))

        with patch(
            "src.surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await FkPost.objects().filter(title="Hello").bulk_update({"author": "bob"})

        _query, variables = mock_client.query.call_args[0]
        assert RecordId(table="fk_users", id="bob") in variables.values()


# ==================== JSON protocol fallback ====================


class TestJSONEncoderRecordId:
    """RecordId must stay serializable when the JSON protocol is selected."""

    def test_record_id_encodes_as_string(self) -> None:
        payload: dict[str, Any] = {"author": RecordId(table="fk_users", id="alice")}
        assert json.loads(json.dumps(payload, cls=SurrealJSONEncoder)) == {"author": "fk_users:alice"}

    def test_datetime_still_encodes(self) -> None:
        payload = {"at": datetime(2026, 8, 23, 10, 0, 0)}
        assert json.loads(json.dumps(payload, cls=SurrealJSONEncoder)) == {"at": "2026-08-23T10:00:00"}


# ==================== Review findings (PR #174) ====================


class TestFilterCollectionTypes:
    """`__in` accepts any collection, including an unhashable-safe set."""

    def test_set_is_accepted(self) -> None:
        """A set of references must not raise — RecordId is unhashable."""
        qs = FkPost.objects().filter(author__in={"fk_users:alice", "fk_users:bob"})
        qs._compile_where_clause()
        bound = qs._variables["_f0"]
        assert isinstance(bound, list)
        assert sorted(str(item) for item in bound) == ["fk_users:alice", "fk_users:bob"]

    def test_tuple_becomes_a_list(self) -> None:
        qs = FkPost.objects().filter(author__in=("alice", "bob"))
        qs._compile_where_clause()
        assert qs._variables["_f0"] == [
            RecordId(table="fk_users", id="alice"),
            RecordId(table="fk_users", id="bob"),
        ]


class TestAliasedForeignKeyColumns:
    """A filter addresses the column, so an alias must resolve too."""

    def test_columns_map_covers_both_spellings(self) -> None:
        assert FkComment.get_foreign_key_columns() == {
            "post": "fk_posts",
            "post_id": "fk_posts",
        }

    def test_filter_by_alias_binds_a_record_id(self) -> None:
        qs = FkComment.objects().filter(post_id="fk_posts:1")
        qs._compile_where_clause()
        assert qs._variables["_f0"] == RecordId(table="fk_posts", id="1")

    def test_filter_by_alias_in_list(self) -> None:
        qs = FkComment.objects().filter(post_id__in=["1", "fk_posts:2"])
        qs._compile_where_clause()
        assert qs._variables["_f0"] == [
            RecordId(table="fk_posts", id="1"),
            RecordId(table="fk_posts", id="2"),
        ]

    def test_filter_by_alias_in_q_object(self) -> None:
        qs = FkComment.objects().filter(Q(post_id="fk_posts:1"))
        qs._compile_where_clause()
        assert qs._variables["_f0"] == RecordId(table="fk_posts", id="1")

    def test_string_lookups_on_an_alias_stay_untouched(self) -> None:
        qs = FkComment.objects().filter(post_id__contains="posts")
        qs._compile_where_clause()
        assert qs._variables["_f0"] == "posts"

    @pytest.mark.asyncio
    async def test_bulk_update_coerces_an_aliased_column(self) -> None:
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(return_value=MagicMock(all_records=[{}]))

        with patch(
            "src.surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await FkComment.objects().filter(body="Nice").bulk_update({"post_id": "1"})

        _query, variables = mock_client.query.call_args[0]
        assert RecordId(table="fk_posts", id="1") in variables.values()


class TestUpsertForeignKeys:
    """upsert()/bulk_upsert() write record links like the other write paths."""

    @pytest.mark.asyncio
    async def test_upsert_defaults_are_coerced(self) -> None:
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(
            return_value=MagicMock(all_records=[{"id": "fk_posts:1", "title": "Hello", "author": "fk_users:alice"}]),
        )

        with patch(
            "src.surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await FkPost.objects().upsert(
                defaults={"title": "Hello", "author": "alice"},
                id="fk_posts:1",
            )

        _query, variables = mock_client.query.call_args[0]
        assert variables["_up_author"] == RecordId(table="fk_users", id="alice")
        assert variables["_up_title"] == "Hello"

    @pytest.mark.asyncio
    async def test_upsert_on_conflict_is_coerced(self) -> None:
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(
            return_value=MagicMock(all_records=[{"id": "fk_posts:1", "title": "Hello", "author": "fk_users:alice"}]),
        )

        with patch(
            "src.surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await FkPost.objects().upsert(
                defaults={"title": "Hello", "author": "alice"},
                id="fk_posts:1",
                on_conflict={"author": FkUser(id="bob", name="Bob")},
            )

        _query, variables = mock_client.query.call_args[0]
        assert variables["_oc_author"] == RecordId(table="fk_users", id="bob")

    @pytest.mark.asyncio
    async def test_bulk_upsert_coerces_instance_data(self) -> None:
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(return_value=MagicMock(all_records=[]))

        posts = [FkPost(id="1", title="Hello", author="fk_users:alice")]
        with patch(
            "src.surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await FkPost.objects().bulk_upsert(posts)

        _query, variables = mock_client.query.call_args[0]
        assert RecordId(table="fk_users", id="alice") in variables.values()

    @pytest.mark.asyncio
    async def test_bulk_upsert_coerces_on_conflict(self) -> None:
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(return_value=MagicMock(all_records=[]))

        posts = [FkPost(id="1", title="Hello", author="fk_users:alice")]
        with patch(
            "src.surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await FkPost.objects().bulk_upsert(posts, on_conflict={"author": "bob"})

        _query, variables = mock_client.query.call_args[0]
        assert variables["_oc0_author"] == RecordId(table="fk_users", id="bob")


class TestUpdateSignalPayload:
    """Signals are a public API and must not see wire types."""

    @pytest.mark.asyncio
    async def test_update_signals_get_uncoerced_values(self) -> None:
        mock_client = AsyncMock()
        mock_client.merge = AsyncMock(return_value=MagicMock(records=[]))
        seen: list[Any] = []

        async def handler(sender: Any, instance: Any, **kwargs: Any) -> None:
            seen.append(kwargs["update_fields"]["author"])

        post = FkPost(id="1", title="Hello", author="fk_users:alice")
        model_signals.pre_update.connect(FkPost)(handler)
        try:
            with patch(
                "src.surreal_orm.model_base.SurrealDBConnectionManager",
                new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
            ):
                await post.update()
        finally:
            model_signals.pre_update.disconnect(handler, FkPost)

        assert seen == ["fk_users:alice"]

    @pytest.mark.asyncio
    async def test_update_still_writes_a_record_id(self) -> None:
        mock_client = AsyncMock()
        mock_client.merge = AsyncMock(return_value=MagicMock(records=[]))

        post = FkPost(id="1", title="Hello", author="fk_users:alice")
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await post.update()

        _thing, data = mock_client.merge.call_args[0]
        assert data["author"] == RecordId(table="fk_users", id="alice")


class TestUnsavedReference:
    """An unsaved instance cannot be referenced — say so explicitly."""

    def test_foreign_key_rejects_an_unsaved_instance(self) -> None:
        with pytest.raises(ValidationError, match="cannot reference an unsaved FkUser"):
            FkPost(title="Hello", author=FkUser(name="Alice"))

    def test_many_to_many_rejects_an_unsaved_instance(self) -> None:
        with pytest.raises(ValidationError, match="cannot reference an unsaved FkGroup"):
            FkMember(groups=[FkGroup()])

    def test_filter_rejects_an_unsaved_instance(self) -> None:
        with pytest.raises(ValueError, match="cannot reference an unsaved FkUser"):
            FkPost.objects().filter(author=FkUser(name="Alice"))._compile_where_clause()

    def test_unrelated_object_with_record_id_passes_through(self) -> None:
        """The guard must not fire on anything that merely has the attribute."""

        class NotAModel:
            record_id = "not-a-record-id"

        obj = NotAModel()
        assert record_link_to_str(obj) is obj

    def test_saved_instance_is_still_accepted(self) -> None:
        post = FkPost(title="Hello", author=FkUser(id="alice", name="Alice"))
        assert post.author == "fk_users:alice"

    def test_none_is_still_accepted(self) -> None:
        assert FkComment(body="Nice", post_id=None).post is None


# ==================== Optional foreign keys (#268) ====================
#
# ``ForeignKey("X") | None`` and ``Optional[ForeignKey("X")]`` make the field's
# annotation a union whose member is the ``Annotated`` type.  Pydantic does not
# lift a union member's metadata into ``FieldInfo.metadata``, so a lookup that
# reads only the metadata never sees the marker: the field was neither written
# as a record link nor filtered as one.  ``FieldInfo.annotation`` still carries
# the marker, which is where the migrations introspector already looked.


class Fk268User(BaseSurrealModel):
    """Target of the optional foreign keys below."""

    model_config = SurrealConfigDict(table_name="fk268_users")

    id: str | None = None
    name: str = Field(default="")


class Fk268PipePost(BaseSurrealModel):
    """ForeignKey wrapped in a redundant ``| None``."""

    model_config = SurrealConfigDict(table_name="fk268_posts")

    id: str | None = None
    title: str = Field(default="")
    author: ForeignKey("Fk268User") | None = None  # type: ignore[valid-type]


class Fk268OptionalPost(BaseSurrealModel):
    """ForeignKey wrapped in ``Optional[...]``."""

    model_config = SurrealConfigDict(table_name="fk268_posts")

    id: str | None = None
    title: str = Field(default="")
    author: Optional[ForeignKey("Fk268User")] = None  # type: ignore[valid-type]


class Fk268PipeAliasedPost(BaseSurrealModel):
    """Optional ForeignKey stored under a different column name."""

    model_config = SurrealConfigDict(table_name="fk268_posts")

    id: str | None = None
    title: str = Field(default="")
    author: ForeignKey("Fk268User") | None = Field(default=None, alias="author_id")  # type: ignore[valid-type]


class Fk268OptionalAliasedPost(BaseSurrealModel):
    """``Optional[ForeignKey(...)]`` stored under a different column name."""

    model_config = SurrealConfigDict(table_name="fk268_posts")

    id: str | None = None
    title: str = Field(default="")
    author: Optional[ForeignKey("Fk268User")] = Field(default=None, alias="author_id")  # type: ignore[valid-type]


_OPTIONAL_FK_MODELS = pytest.mark.parametrize(
    "model",
    [Fk268PipePost, Fk268OptionalPost],
    ids=["pipe-none", "optional"],
)
_ALIASED_OPTIONAL_FK_MODELS = pytest.mark.parametrize(
    "model",
    [Fk268PipeAliasedPost, Fk268OptionalAliasedPost],
    ids=["pipe-none", "optional"],
)


class TestOptionalForeignKeyDiscovery:
    """An optional ForeignKey is discovered like a required one."""

    @_OPTIONAL_FK_MODELS
    def test_targets(self, model: type[BaseSurrealModel]) -> None:
        assert model.get_foreign_key_targets() == {"author": "fk268_users"}

    @_OPTIONAL_FK_MODELS
    def test_columns(self, model: type[BaseSurrealModel]) -> None:
        assert model.get_foreign_key_columns() == {"author": "fk268_users"}

    @_ALIASED_OPTIONAL_FK_MODELS
    def test_aliased_targets(self, model: type[BaseSurrealModel]) -> None:
        assert model.get_foreign_key_targets() == {"author": "fk268_users"}

    @_ALIASED_OPTIONAL_FK_MODELS
    def test_aliased_columns_cover_both_spellings(self, model: type[BaseSurrealModel]) -> None:
        assert model.get_foreign_key_columns() == {
            "author": "fk268_users",
            "author_id": "fk268_users",
        }

    @_OPTIONAL_FK_MODELS
    def test_instance_is_stored_as_string(self, model: type[BaseSurrealModel]) -> None:
        """Assignment already went through the marker's validator."""
        post = model(title="Hello", author=Fk268User(id="alice"))
        assert post.author == "fk268_users:alice"  # type: ignore[attr-defined]


class TestOptionalForeignKeyOnWrite:
    """An optional ForeignKey leaves the ORM as a record link."""

    @_OPTIONAL_FK_MODELS
    def test_full_id_is_wrapped(self, model: type[BaseSurrealModel]) -> None:
        data = model(title="Hello")._coerce_foreign_keys({"author": "fk268_users:alice"})
        assert data["author"] == RecordId(table="fk268_users", id="alice")

    @_OPTIONAL_FK_MODELS
    def test_bare_id_is_qualified(self, model: type[BaseSurrealModel]) -> None:
        data = model(title="Hello")._coerce_foreign_keys({"author": "alice"})
        assert data["author"] == RecordId(table="fk268_users", id="alice")

    @_OPTIONAL_FK_MODELS
    def test_instance_is_wrapped(self, model: type[BaseSurrealModel]) -> None:
        data = model(title="Hello")._coerce_foreign_keys({"author": Fk268User(id="alice")})
        assert data["author"] == RecordId(table="fk268_users", id="alice")

    @_OPTIONAL_FK_MODELS
    def test_none_is_preserved(self, model: type[BaseSurrealModel]) -> None:
        data = model(title="Hello")._coerce_foreign_keys({"author": None, "title": "Hello"})
        assert data == {"author": None, "title": "Hello"}

    @_ALIASED_OPTIONAL_FK_MODELS
    def test_aliased_key_is_wrapped(self, model: type[BaseSurrealModel]) -> None:
        data = model(title="Hello")._coerce_foreign_keys({"author_id": "alice"})
        assert data["author_id"] == RecordId(table="fk268_users", id="alice")

    @pytest.mark.asyncio
    @_OPTIONAL_FK_MODELS
    async def test_save_sends_record_id(self, model: type[BaseSurrealModel]) -> None:
        mock_client = AsyncMock()
        mock_client.create = AsyncMock(
            return_value=MagicMock(exists=True, record={"id": "fk268_posts:1", "title": "Hello"}),
        )

        post = model(title="Hello", author="fk268_users:alice")
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await post.save()

        _table, data = mock_client.create.call_args[0]
        assert data["author"] == RecordId(table="fk268_users", id="alice")

    @pytest.mark.asyncio
    @_OPTIONAL_FK_MODELS
    async def test_merge_accepts_an_instance(self, model: type[BaseSurrealModel]) -> None:
        mock_client = AsyncMock()
        mock_client.merge = AsyncMock(return_value=MagicMock(is_empty=False))

        post = model(id="1", title="Hello")
        post._db_persisted = True
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await post.merge(refresh=False, author=Fk268User(id="bob"))

        _thing, data = mock_client.merge.call_args[0]
        assert data["author"] == RecordId(table="fk268_users", id="bob")

    @pytest.mark.asyncio
    @_OPTIONAL_FK_MODELS
    async def test_bulk_update_binds_record_id(self, model: type[BaseSurrealModel]) -> None:
        mock_client = AsyncMock()
        mock_client.query = AsyncMock(return_value=MagicMock(all_records=[{}]))

        with patch(
            "src.surreal_orm.query_set.SurrealDBConnectionManager",
            new=MagicMock(get_client=AsyncMock(return_value=mock_client)),
        ):
            await model.objects().filter(title="Hello").bulk_update({"author": "bob"})

        _query, variables = mock_client.query.call_args[0]
        assert RecordId(table="fk268_users", id="bob") in variables.values()


class TestOptionalForeignKeyInFilters:
    """An optional ForeignKey filter binds a record link, not a string."""

    @_OPTIONAL_FK_MODELS
    @pytest.mark.parametrize(
        "value",
        ["fk268_users:alice", "alice", Fk268User(id="alice")],
        ids=["full-id", "bare-id", "instance"],
    )
    def test_exact(self, model: type[BaseSurrealModel], value: Any) -> None:
        qs = model.objects().filter(author=value)
        qs._compile_where_clause()
        assert qs._variables["_f0"] == RecordId(table="fk268_users", id="alice")

    @_OPTIONAL_FK_MODELS
    def test_in_converts_each_element(self, model: type[BaseSurrealModel]) -> None:
        qs = model.objects().filter(author__in=[Fk268User(id="alice"), "fk268_users:bob", "carol"])
        qs._compile_where_clause()
        assert qs._variables["_f0"] == [
            RecordId(table="fk268_users", id="alice"),
            RecordId(table="fk268_users", id="bob"),
            RecordId(table="fk268_users", id="carol"),
        ]

    @_OPTIONAL_FK_MODELS
    def test_not_in_converts_each_element(self, model: type[BaseSurrealModel]) -> None:
        qs = model.objects().filter(author__not_in=("spam", "fk268_users:ham"))
        query = qs._compile_query()
        assert "author NOT IN $_f0" in query
        assert qs._variables["_f0"] == [
            RecordId(table="fk268_users", id="spam"),
            RecordId(table="fk268_users", id="ham"),
        ]

    @_OPTIONAL_FK_MODELS
    def test_q_object(self, model: type[BaseSurrealModel]) -> None:
        qs = model.objects().filter(Q(author="alice") | ~Q(author__in=["fk268_users:bob"]))
        qs._compile_where_clause()
        assert qs._variables["_f0"] == RecordId(table="fk268_users", id="alice")
        assert qs._variables["_f1"] == [RecordId(table="fk268_users", id="bob")]

    @_ALIASED_OPTIONAL_FK_MODELS
    def test_filter_by_alias(self, model: type[BaseSurrealModel]) -> None:
        qs = model.objects().filter(Q(author_id="alice"), author_id__in=["fk268_users:bob"])
        qs._compile_where_clause()
        assert RecordId(table="fk268_users", id="alice") in qs._variables.values()
        assert [RecordId(table="fk268_users", id="bob")] in qs._variables.values()

    @_OPTIONAL_FK_MODELS
    def test_isnull_is_untouched(self, model: type[BaseSurrealModel]) -> None:
        qs = model.objects().filter(author__isnull=True)
        assert "author IS NULL" in qs._compile_query()

    @_OPTIONAL_FK_MODELS
    def test_variable_reference_is_untouched(self, model: type[BaseSurrealModel]) -> None:
        qs = model.objects().filter(author="$who").variables(who="fk268_users:alice")
        assert "author = $who" in qs._compile_query()
        assert qs._variables["who"] == "fk268_users:alice"

    @_OPTIONAL_FK_MODELS
    def test_string_lookups_are_untouched(self, model: type[BaseSurrealModel]) -> None:
        qs = model.objects().filter(author__startswith="fk268_users:a")
        qs._compile_where_clause()
        assert qs._variables["_f0"] == "fk268_users:a"


# ==================== Shared marker lookup (#268) ====================
#
# The migrations introspector and get_foreign_key_targets() read the same
# relation markers.  They used two lookups, and only one of them searched union
# members.  These tests pin what the introspector finds — and the DDL it emits —
# for every marker, bare and wrapped in a union, so moving both readers onto one
# helper changes nothing on the migrations side.


class Fk268Shelf(BaseSurrealModel):
    """Every relation marker, bare."""

    model_config = SurrealConfigDict(table_name="fk268_shelves")

    id: str | None = None
    owner: ForeignKey("Fk268User", on_delete="SET_NULL")  # type: ignore[valid-type]
    readers: ManyToMany("Fk268User")  # type: ignore[valid-type]
    likes: Relation("fk268_likes", "Fk268User")  # type: ignore[valid-type]
    books: ReferencesField["fk268_books"]  # noqa: F821, UP037


class Fk268PipeShelf(BaseSurrealModel):
    """Every relation marker, wrapped in a redundant ``| None``."""

    model_config = SurrealConfigDict(table_name="fk268_shelves")

    id: str | None = None
    owner: ForeignKey("Fk268User", on_delete="SET_NULL") | None = None  # type: ignore[valid-type]
    readers: ManyToMany("Fk268User") | None = None  # type: ignore[valid-type]
    likes: Relation("fk268_likes", "Fk268User") | None = None  # type: ignore[valid-type]
    books: ReferencesField["fk268_books"] | None = None  # noqa: F821, UP037


class Fk268OptionalShelf(BaseSurrealModel):
    """Every relation marker, wrapped in ``Optional[...]``."""

    model_config = SurrealConfigDict(table_name="fk268_shelves")

    id: str | None = None
    owner: Optional[ForeignKey("Fk268User", on_delete="SET_NULL")] = None  # type: ignore[valid-type]
    readers: Optional[ManyToMany("Fk268User")] = None  # type: ignore[valid-type]
    likes: Optional[Relation("fk268_likes", "Fk268User")] = None  # type: ignore[valid-type]
    books: Optional[ReferencesField["fk268_books"]] = None  # noqa: F821, UP037


_SHELF_MODELS = pytest.mark.parametrize(
    "model",
    [Fk268Shelf, Fk268PipeShelf, Fk268OptionalShelf],
    ids=["bare", "pipe-none", "optional"],
)

#: What the introspector emitted for these fields before the lookup was shared.
_SHELF_DDL = {
    "owner": "DEFINE FIELD owner ON fk268_shelves TYPE option<record<fk268_users>> REFERENCE ON DELETE UNSET;",
    "books": "DEFINE FIELD books ON fk268_shelves TYPE option<array<record<fk268_books>>> REFERENCE;",
}


class TestSharedMarkerLookup:
    """The introspector finds the same markers through the shared lookup."""

    @_SHELF_MODELS
    @pytest.mark.parametrize(
        ("field", "marker_type"),
        [
            ("owner", _ForeignKeyMarker),
            ("readers", _ManyToManyMarker),
            ("likes", _RelationMarker),
            ("books", _ReferencesMarker),
        ],
    )
    def test_introspector_finds_the_marker(
        self,
        model: type[BaseSurrealModel],
        field: str,
        marker_type: type,
    ) -> None:
        field_info = model.model_fields[field]
        # The introspector passes the get_type_hints() annotation, which keeps
        # Annotated, and falls back to FieldInfo.annotation, which does not.
        hints = get_type_hints(model, include_extras=True)
        for annotation in (hints[field], field_info.annotation):
            assert isinstance(_get_relation_marker(annotation, field_info), marker_type)

    @_SHELF_MODELS
    def test_virtual_fields_define_no_column(self, model: type[BaseSurrealModel]) -> None:
        fields = ModelIntrospector([model]).introspect().tables["fk268_shelves"].fields
        assert set(fields) == {"owner", "books"}

    @_SHELF_MODELS
    def test_define_field_is_unchanged(self, model: type[BaseSurrealModel]) -> None:
        fields = ModelIntrospector([model]).introspect().tables["fk268_shelves"].fields
        ddl = {name: AddField.from_field_state("fk268_shelves", state).forwards() for name, state in fields.items()}
        assert ddl == _SHELF_DDL

    @_SHELF_MODELS
    def test_both_readers_agree(self, model: type[BaseSurrealModel]) -> None:
        """Record-link coercion sees exactly the foreign keys migrations see."""
        hints = get_type_hints(model, include_extras=True)
        introspected = {
            name
            for name, field_info in model.model_fields.items()
            if isinstance(_get_relation_marker(hints[name], field_info), _ForeignKeyMarker)
        }
        assert set(model.get_foreign_key_targets()) == introspected == {"owner"}
