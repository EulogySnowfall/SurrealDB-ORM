"""
Unit tests for the #174 backport onto the v2 (SurrealDB 2.6.x) line.

Reproduced against a live SurrealDB 2.6.5 before porting:

  1) save() with a "table:id" string into a record<> column
     -> QueryError: Found 'p174_authors:alice' for field `author`
  2) filter by the same string
     -> rows found = 0  (the row demonstrably exists)

The second is the dangerous one: a wrong answer rather than an error. A
``ForeignKey`` holds a ``"table:id"`` string in Python, and nothing converted it
at the wire boundary, so a ``record<>`` column rejected the write and a filter
compared a string against a record value.
"""

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.fields import ForeignKey
from src.surreal_orm.model_base import record_link_to_str, to_record_id
from surreal_sdk.protocol.cbor import RecordId


class FkTarget(BaseSurrealModel):
    """Target model, whose table name differs from the class name."""

    model_config = SurrealConfigDict(table_name="fk_targets")

    id: str | None = None
    name: str = "x"


class FkHolder(BaseSurrealModel):
    """Holds the foreign key, including an aliased one."""

    model_config = SurrealConfigDict(table_name="fk_holders")

    id: str | None = None
    title: str = "t"
    author: ForeignKey("FkTarget") = None


class TestToRecordId:
    """A ``record<>`` column rejects a bound string."""

    def test_a_full_record_id_string_becomes_a_record_id(self) -> None:
        """The conversion the wire boundary was missing."""
        converted = to_record_id("fk_targets:alice")

        assert isinstance(converted, RecordId)
        assert converted.table == "fk_targets"
        assert converted.id == "alice"

    def test_a_bare_id_is_left_alone(self) -> None:
        """Without a table there is nothing to qualify it with."""
        assert to_record_id("alice") == "alice"

    def test_a_non_string_is_left_alone(self) -> None:
        """Only strings can be record-ID strings."""
        assert to_record_id(42) == 42


class TestRecordLinkToStr:
    """The inverse, used when a model instance is passed as the value."""

    def test_a_record_id_renders_as_table_colon_id(self) -> None:
        """Inverse of to_record_id."""
        assert record_link_to_str(RecordId(table="fk_targets", id="alice")) == "fk_targets:alice"

    def test_an_unsaved_instance_raises_a_clear_error(self) -> None:
        """Referencing an unsaved record cannot work; say so plainly."""
        with pytest.raises(ValueError, match="unsaved"):
            record_link_to_str(FkTarget(name="nobody"))

    def test_a_plain_string_is_left_alone(self) -> None:
        """Already a record link."""
        assert record_link_to_str("fk_targets:alice") == "fk_targets:alice"


class TestForeignKeyTargets:
    """Resolving where a foreign key points, including under an alias."""

    def test_targets_map_the_field_to_its_table(self) -> None:
        """``ForeignKey("FkTarget")`` names the model, not the table."""
        assert FkHolder.get_foreign_key_targets() == {"author": "fk_targets"}

    def test_columns_include_the_field_name(self) -> None:
        """Filters and bulk writes address database columns."""
        assert FkHolder.get_foreign_key_columns()["author"] == "fk_targets"


class TestRecordIdProperty:
    """``instance.record_id`` is the ``Model.pk`` analog."""

    def test_a_saved_instance_exposes_its_record_id(self) -> None:
        """Bindable against a record<> column in a raw query."""
        rid = FkTarget(id="alice", name="Alice").record_id

        assert isinstance(rid, RecordId)
        assert str(rid) == "fk_targets:alice"

    def test_an_unsaved_instance_has_none(self) -> None:
        """No id, no identity."""
        assert FkTarget(name="Alice").record_id is None
