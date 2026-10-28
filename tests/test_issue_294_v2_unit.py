"""
Unit tests for #294 on the v2 line: an optional ``ForeignKey`` was never coerced.

``author: ForeignKey("X") | None`` and ``Optional[ForeignKey("X")]`` keep their
``Annotated`` marker on the union member: Pydantic leaves it on
``FieldInfo.annotation`` and does not lift it into ``FieldInfo.metadata``.
``get_foreign_key_targets()`` read only the metadata, so such a field was
absent from the map every record-link coercion is built on, and its value
reached the server as a string.

Reproduced against a live SurrealDB 2.7.0 before the fix:

  1) save() with ``"table:id"`` into an ``option<record<...>>`` column
     -> QueryError: Found '...:alice' for field `author` ... but expected a
        option<record<...>>
  2) filter(author=...) / filter(author__in=[...]) on a row created server-side
     -> rows found = 0  (the row exists)
  3) SCHEMALESS table -> stored the string, not a link
"""

from typing import Any, Optional

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, Q, SurrealConfigDict
from src.surreal_orm.fields import ForeignKey
from src.surreal_orm.model_base import _to_record_link
from surreal_sdk.protocol.cbor import RecordId

AUTHORS = "i294u_authors"


class I294UAuthor(BaseSurrealModel):
    """Target model, whose table name differs from the class name."""

    model_config = SurrealConfigDict(table_name=AUTHORS)

    id: str | None = None
    name: str = "a"


class I294UPipeBook(BaseSurrealModel):
    """``ForeignKey(...) | None``, plain and aliased."""

    model_config = SurrealConfigDict(table_name="i294u_pipe_books", populate_by_name=True)

    id: str | None = None
    title: str = "t"
    author: ForeignKey("I294UAuthor") | None = None
    editor: ForeignKey("I294UAuthor") | None = Field(default=None, alias="editor_id")


class I294UOptionalBook(BaseSurrealModel):
    """``Optional[ForeignKey(...)]``, plain and aliased."""

    model_config = SurrealConfigDict(table_name="i294u_optional_books", populate_by_name=True)

    id: str | None = None
    title: str = "t"
    author: Optional[ForeignKey("I294UAuthor")] = None  # the Optional spelling is the case under test
    editor: Optional[ForeignKey("I294UAuthor")] = Field(default=None, alias="editor_id")


class I294UBareBook(BaseSurrealModel):
    """The non-optional form, coerced since the #174 backport — must not move."""

    model_config = SurrealConfigDict(table_name="i294u_bare_books")

    id: str | None = None
    author: ForeignKey("I294UAuthor") = None


OPTIONAL_MODELS = [I294UPipeBook, I294UOptionalBook]


def _alice() -> RecordId:
    return RecordId(table=AUTHORS, id="alice")


@pytest.mark.parametrize("model", OPTIONAL_MODELS, ids=["pipe", "Optional"])
class TestForeignKeyLookup:
    """The map every coercion reads must include an optional foreign key."""

    def test_targets_include_the_optional_field(self, model: type[BaseSurrealModel]) -> None:
        """Both the plain and the aliased field point at the target's table."""
        assert model.get_foreign_key_targets() == {"author": AUTHORS, "editor": AUTHORS}

    def test_columns_include_the_field_name_and_the_alias(self, model: type[BaseSurrealModel]) -> None:
        """Filters address columns, so the alias resolves too."""
        assert model.get_foreign_key_columns() == {
            "author": AUTHORS,
            "editor": AUTHORS,
            "editor_id": AUTHORS,
        }


class TestBareForeignKeyUnchanged:
    """The required form keeps resolving exactly as before."""

    def test_targets(self) -> None:
        assert I294UBareBook.get_foreign_key_targets() == {"author": AUTHORS}


@pytest.mark.parametrize("model", OPTIONAL_MODELS, ids=["pipe", "Optional"])
class TestWriteCoercion:
    """``_coerce_foreign_keys()`` feeds save(), update() and merge()."""

    @pytest.mark.parametrize(
        "value",
        [f"{AUTHORS}:alice", "alice", I294UAuthor(id="alice")],
        ids=["full-id", "bare-id", "instance"],
    )
    def test_value_becomes_a_record_id(self, model: type[BaseSurrealModel], value: Any) -> None:
        """All three accepted forms reach the wire as the same record link."""
        instance = model(title="t")
        coerced = instance._coerce_foreign_keys({"author": value})

        assert coerced["author"] == _alice()
        assert isinstance(coerced["author"], RecordId)

    def test_aliased_key_is_coerced(self, model: type[BaseSurrealModel]) -> None:
        """``model_dump(by_alias=True)`` keys the value by its alias."""
        coerced = model(title="t")._coerce_foreign_keys({"editor_id": "alice"})

        assert coerced["editor_id"] == _alice()

    def test_none_stays_none(self, model: type[BaseSurrealModel]) -> None:
        """An unset optional link is not a record."""
        assert model(title="t")._coerce_foreign_keys({"author": None}) == {"author": None}

    def test_dump_of_an_instance_is_coerced(self, model: type[BaseSurrealModel]) -> None:
        """The value save() actually serializes — a model instance assigned to the field."""
        instance = model(title="t", author=I294UAuthor(id="alice"))
        coerced = instance._coerce_foreign_keys(instance.model_dump(exclude={"id"}))

        assert coerced["author"] == _alice()


@pytest.mark.parametrize("model", OPTIONAL_MODELS, ids=["pipe", "Optional"])
class TestFilterCoercion:
    """``exact`` / ``in`` / ``not_in`` compare whole records, so they bind RecordIds."""

    @staticmethod
    def _bound(model: type[BaseSurrealModel], *args: Any, **kwargs: Any) -> tuple[list[str], dict[str, Any]]:
        return model.objects().filter(*args, **kwargs)._build_where_parts()

    @pytest.mark.parametrize(
        "value",
        [f"{AUTHORS}:alice", "alice", I294UAuthor(id="alice")],
        ids=["full-id", "bare-id", "instance"],
    )
    def test_exact(self, model: type[BaseSurrealModel], value: Any) -> None:
        parts, variables = self._bound(model, author=value)

        assert parts == ["author = $_f0"]
        assert variables == {"_f0": _alice()}

    @pytest.mark.parametrize("lookup", ["in", "not_in"])
    def test_in_and_not_in_coerce_each_element(self, model: type[BaseSurrealModel], lookup: str) -> None:
        _, variables = self._bound(model, **{f"author__{lookup}": ["alice", f"{AUTHORS}:bob", I294UAuthor(id="carol")]})

        assert variables == {
            "_f0": [_alice(), RecordId(table=AUTHORS, id="bob"), RecordId(table=AUTHORS, id="carol")],
        }

    def test_inside_q(self, model: type[BaseSurrealModel]) -> None:
        _, variables = self._bound(model, Q(author="alice") | Q(author__in=["bob"]))

        assert variables == {"_f0": _alice(), "_f1": [RecordId(table=AUTHORS, id="bob")]}

    def test_aliased_column(self, model: type[BaseSurrealModel]) -> None:
        """Filtering by the real column name — the alias — is coerced too."""
        _, variables = self._bound(model, editor_id="alice")

        assert variables == {"_f0": _alice()}

    def test_isnull_is_left_alone(self, model: type[BaseSurrealModel]) -> None:
        parts, variables = self._bound(model, author__isnull=True)

        assert parts == ["author IS NULL"]
        assert variables == {}

    def test_variable_reference_is_left_alone(self, model: type[BaseSurrealModel]) -> None:
        """``$var`` is the caller's own binding, not a value to wrap."""
        assert model.objects()._coerce_filter_value("author", "exact", "$who") == "$who"

    @pytest.mark.parametrize("lookup", ["contains", "startswith", "regex"])
    def test_string_lookups_are_left_alone(self, model: type[BaseSurrealModel], lookup: str) -> None:
        """They match on the raw value, not the record."""
        assert model.objects()._coerce_filter_value("author", lookup, "alice") == "alice"


def test_to_record_link_none_is_unchanged() -> None:
    """None is never qualified into a record, even with a known table."""
    assert _to_record_link(None, AUTHORS) is None
