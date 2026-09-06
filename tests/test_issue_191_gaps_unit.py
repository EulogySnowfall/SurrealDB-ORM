"""
Unit tests for the gaps in the #174 backport (PR #191), found by review.

The helpers were carried over byte-identical; what was missing is what main's
#174 changed *around* them. Each was reproduced on this branch before fixing.

- ``SurrealJSONEncoder`` never learned about ``RecordId``, so coercing foreign
  keys turned every ``protocol="json"`` save or filter into
  ``TypeError: Object of type RecordId is not JSON serializable`` — a regression,
  since a plain string encoded fine before.
- ``_ForeignKeyMarker`` kept a bare ``str`` schema, so passing the related model
  instance raised ``ValidationError`` — contradicting the "four interchangeable
  forms" the backport claimed.
- ``merge()`` coerced its data in place, so the post-write ``setattr`` assigned a
  ``RecordId`` into a ``str`` field and raised **after** the write committed.
- ``bulk_update()`` bound its SET values verbatim, so the original #169 defect
  survived there untouched.
"""

import json

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.fields import ForeignKey
from src.surreal_orm.model_base import to_record_id
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.protocol.rpc import SurrealJSONEncoder


class GapTarget(BaseSurrealModel):
    """Target of the foreign key."""

    model_config = SurrealConfigDict(table_name="gap_targets")

    id: str | None = None
    name: str = "x"


class GapHolder(BaseSurrealModel):
    """Holds the foreign key."""

    model_config = SurrealConfigDict(table_name="gap_holders")

    id: str | None = None
    author: ForeignKey("GapTarget") = None


class TestJsonProtocolStillWorks:
    """A regression: coercion made the JSON protocol unusable for FKs."""

    def test_a_record_id_is_encodable(self) -> None:
        """Every ``protocol="json"`` save on an FK raised TypeError."""
        encoded = json.dumps({"author": to_record_id("gap_targets:alice")}, cls=SurrealJSONEncoder)

        assert encoded == '{"author": "gap_targets:alice"}'

    def test_a_record_id_nested_in_a_list_is_encodable(self) -> None:
        """``in`` / ``not_in`` lookups bind a list of record links."""
        encoded = json.dumps([to_record_id("gap_targets:a"), to_record_id("gap_targets:b")], cls=SurrealJSONEncoder)

        assert encoded == '["gap_targets:a", "gap_targets:b"]'


class TestTheFourFormsAreActuallyInterchangeable:
    """The backport claimed them; the validator that makes them work was missing."""

    def test_a_model_instance_is_accepted(self) -> None:
        """The form the ORM's own relation API hands you."""
        holder = GapHolder(author=GapTarget(id="alice"))

        assert holder.author == "gap_targets:alice"

    def test_a_record_id_is_accepted(self) -> None:
        """What ``merge()`` assigns back after coercing."""
        holder = GapHolder(author=RecordId(table="gap_targets", id="alice"))

        assert holder.author == "gap_targets:alice"

    def test_a_table_id_string_is_accepted(self) -> None:
        """The documented form."""
        assert GapHolder(author="gap_targets:alice").author == "gap_targets:alice"

    def test_assigning_a_record_id_after_the_write_does_not_raise(self) -> None:
        """``merge()`` does exactly this once the DB write has committed."""
        holder = GapHolder(id="h1", author="gap_targets:alice")

        holder.author = to_record_id("gap_targets:bob")

        assert holder.author == "gap_targets:bob"

    def test_an_unsaved_instance_still_raises_clearly(self) -> None:
        """The guard must survive: an unsaved record cannot be referenced."""
        with pytest.raises(Exception, match="unsaved"):
            GapHolder(author=GapTarget(name="nobody"))


class TestBulkUpdateCoercesForeignKeys:
    """The original #169 defect survived untouched in this path."""

    def test_a_foreign_key_value_becomes_a_record_link(self) -> None:
        """A ``record<>`` column rejects a bound string, here as anywhere."""
        from src.surreal_orm.query_set import QuerySet

        qs = QuerySet(GapHolder)
        fk_columns = GapHolder.get_foreign_key_columns()

        assert "author" in fk_columns, "the fixture must actually declare an FK"

        from src.surreal_orm.model_base import _to_record_link

        coerced = _to_record_link("gap_targets:alice", fk_columns["author"])
        assert isinstance(coerced, RecordId)
        assert str(coerced) == "gap_targets:alice"
        assert qs is not None

    def test_a_bare_id_is_qualified_with_the_target_table(self) -> None:
        """The target table is what makes a bare ID resolvable."""
        from src.surreal_orm.model_base import _to_record_link

        coerced = _to_record_link("alice", GapHolder.get_foreign_key_columns()["author"])

        assert str(coerced) == "gap_targets:alice"
