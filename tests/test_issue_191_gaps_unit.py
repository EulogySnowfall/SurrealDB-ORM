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
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.surreal_orm.model_base import to_record_id
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.protocol.rpc import SurrealJSONEncoder

# The fixtures from the #174 backport's own tests: FkTarget/FkHolder are the
# same pair, so a second near-identical one would only be another thing to keep
# in step.
from tests.test_issue_174_v2_unit import FkHolder as GapHolder
from tests.test_issue_174_v2_unit import FkTarget as GapTarget


class TestJsonProtocolStillWorks:
    """A regression: coercion made the JSON protocol unusable for FKs."""

    def test_a_record_id_is_encodable(self) -> None:
        """Every ``protocol="json"`` save on an FK raised TypeError."""
        encoded = json.dumps({"author": to_record_id("fk_targets:alice")}, cls=SurrealJSONEncoder)

        assert encoded == '{"author": "fk_targets:alice"}'

    def test_a_record_id_nested_in_a_list_is_encodable(self) -> None:
        """``in`` / ``not_in`` lookups bind a list of record links."""
        encoded = json.dumps([to_record_id("fk_targets:a"), to_record_id("fk_targets:b")], cls=SurrealJSONEncoder)

        assert encoded == '["fk_targets:a", "fk_targets:b"]'


class TestTheFourFormsAreActuallyInterchangeable:
    """The backport claimed them; the validator that makes them work was missing."""

    def test_a_model_instance_is_accepted(self) -> None:
        """The form the ORM's own relation API hands you."""
        holder = GapHolder(author=GapTarget(id="alice"))

        assert holder.author == "fk_targets:alice"

    def test_a_record_id_is_accepted(self) -> None:
        """What ``merge()`` assigns back after coercing."""
        holder = GapHolder(author=RecordId(table="fk_targets", id="alice"))

        assert holder.author == "fk_targets:alice"

    def test_a_table_id_string_is_accepted(self) -> None:
        """The documented form."""
        assert GapHolder(author="fk_targets:alice").author == "fk_targets:alice"

    def test_assigning_a_record_id_after_the_write_does_not_raise(self) -> None:
        """``merge()`` does exactly this once the DB write has committed."""
        holder = GapHolder(id="h1", author="fk_targets:alice")

        holder.author = to_record_id("fk_targets:bob")

        assert holder.author == "fk_targets:bob"

    def test_an_unsaved_instance_still_raises_clearly(self) -> None:
        """The guard must survive: an unsaved record cannot be referenced."""
        with pytest.raises(Exception, match="unsaved"):
            GapHolder(author=GapTarget(name="nobody"))


class TestBulkUpdateCoercesForeignKeys:
    """The original #169 defect survived untouched in this path."""

    async def test_the_bound_value_is_a_record_link(self) -> None:
        """Asserted on what ``bulk_update()`` actually binds, not on the helper.

        The first version of this test exercised ``_to_record_link`` directly and
        never called ``bulk_update()``, so deleting the coercion left it green.
        """
        client = AsyncMock()
        client.query = AsyncMock(return_value=SimpleNamespace(results=[], all_records=[]))

        with patch(
            "src.surreal_orm.query_set.SurrealDBConnectionManager.get_client",
            new_callable=AsyncMock,
            return_value=client,
        ):
            await GapHolder.objects().filter(title="x").bulk_update({"author": "alice"})

        _query, variables = client.query.await_args.args[:2]
        bound = variables["_bu0"]

        assert isinstance(bound, RecordId), f"bound a bare {type(bound).__name__}"
        assert str(bound) == "fk_targets:alice"

    def test_a_bare_id_is_qualified_with_the_target_table(self) -> None:
        """The target table is what makes a bare ID resolvable."""
        from src.surreal_orm.model_base import _to_record_link

        coerced = _to_record_link("alice", GapHolder.get_foreign_key_columns()["author"])

        assert str(coerced) == "fk_targets:alice"
