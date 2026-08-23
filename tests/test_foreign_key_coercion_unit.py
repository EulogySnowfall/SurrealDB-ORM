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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field
from pydantic_core import ValidationError

from src.surreal_orm.fields.relation import ForeignKey, ManyToMany
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, to_record_id
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


class TestGetRecordLink:
    """Tests for the model's own "table:id" accessor."""

    def test_saved_instance(self) -> None:
        assert FkUser(id="alice").get_record_link() == "fk_users:alice"

    def test_unsaved_instance(self) -> None:
        assert FkUser(name="Alice").get_record_link() is None


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
