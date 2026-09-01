"""
Unit tests for issue #170 — migration introspection drops relation markers,
virtual fields and nullability.

Four related defects in the model → ``SchemaState`` → operation path:

1. ``_introspect_field`` never unwrapped ``Annotated``, so every ``ForeignKey``
   and ``ReferencesField`` fell through to ``any``.
2. ``ManyToMany`` / ``Relation`` are virtual and were emitted as ``any`` columns.
3. ``FieldState.nullable`` was dropped at the diff boundary — ``AddField`` /
   ``AlterField`` had no ``nullable`` parameter.
4. The "add field to an existing table" diff branch omitted ``reference`` /
   ``on_delete`` that the new-table branch included.
"""

import pytest

from src.surreal_orm.fields import ForeignKey, ManyToMany, ReferencesField, Relation
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
)


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear model registry before each test."""
    clear_model_registry()


class TestForeignKeyIntrospection:
    """Defect 1 — ForeignKey must introspect as a typed record link."""

    def test_foreign_key_maps_to_record_of_target_table(self) -> None:
        """A ForeignKey emits ``record<table>``, not ``any``."""

        class FkUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            name: str

        class FkPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("FkUser")

        state = ModelIntrospector([FkPost]).introspect()
        field = state.tables["posts"].fields["author"]

        assert field.field_type == "record<users>"

    def test_foreign_key_is_nullable(self) -> None:
        """A ForeignKey holds ``str | None``, so the column is optional."""

        class NullUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None

        class NullPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("NullUser")

        state = ModelIntrospector([NullPost]).introspect()

        assert state.tables["posts"].fields["author"].nullable is True

    def test_foreign_key_carries_reference_and_on_delete(self) -> None:
        """``on_delete`` reaches ``FieldState`` so the REFERENCE clause is emitted."""

        class RefUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None

        class RefPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("RefUser", on_delete="SET_NULL")

        state = ModelIntrospector([RefPost]).introspect()
        field = state.tables["posts"].fields["author"]

        assert field.reference is True
        assert field.on_delete == "SET_NULL"

    def test_foreign_key_accepts_surrealdb_on_delete_vocabulary(self) -> None:
        """SurrealDB's own keywords are accepted, so a no-op FK is expressible."""

        class IgnUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None

        class IgnPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("IgnUser", on_delete="IGNORE")

        state = ModelIntrospector([IgnPost]).introspect()

        assert state.tables["posts"].fields["author"].on_delete == "IGNORE"

    def test_foreign_key_to_unknown_model_falls_back_to_untyped_record(self) -> None:
        """An unresolvable target yields ``record`` rather than an invented table."""

        class OrphanPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("NoSuchModelAnywhere")

        state = ModelIntrospector([OrphanPost]).introspect()

        assert state.tables["posts"].fields["author"].field_type == "record"

    def test_foreign_key_target_accepts_a_table_name(self) -> None:
        """``ForeignKey`` may name the table directly instead of the model."""

        class TblUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None

        class TblPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("users")

        state = ModelIntrospector([TblPost]).introspect()

        assert state.tables["posts"].fields["author"].field_type == "record<users>"


class TestReferencesFieldIntrospection:
    """Defect 1 — ReferencesField must introspect as an array of record links."""

    def test_references_field_maps_to_array_of_records(self) -> None:
        """``ReferencesField["books"]`` emits ``array<record<books>>``."""

        class RefAuthor(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="authors")
            id: str | None = None
            # ruff reads the subscript as a forward reference, hence the noqa
            books: ReferencesField["books"]  # noqa: F821

        state = ModelIntrospector([RefAuthor]).introspect()
        field = state.tables["authors"].fields["books"]

        assert field.field_type == "array<record<books>>"
        assert field.nullable is True
        assert field.reference is True

    def test_references_field_carries_on_delete(self) -> None:
        """The ON DELETE strategy given to ``ReferencesField`` is preserved."""

        class CascadeLicense(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="licenses")
            id: str | None = None
            owners: ReferencesField["person", "CASCADE"]  # noqa: F821

        state = ModelIntrospector([CascadeLicense]).introspect()

        assert state.tables["licenses"].fields["owners"].on_delete == "CASCADE"


class TestVirtualFieldsExcluded:
    """Defect 2 — ManyToMany and Relation are virtual and carry no DDL."""

    def test_many_to_many_is_not_a_column(self) -> None:
        """A ManyToMany field never reaches the table state."""

        class M2mGroup(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="groups")
            id: str | None = None

        class M2mUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            name: str
            groups: ManyToMany("M2mGroup", through="membership")

        state = ModelIntrospector([M2mUser]).introspect()
        fields = state.tables["users"].fields

        assert "groups" not in fields
        assert "name" in fields

    def test_graph_relation_is_not_a_column(self) -> None:
        """A graph Relation field never reaches the table state."""

        class RelUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None
            name: str
            following: Relation("follows", "RelUser")

        state = ModelIntrospector([RelUser]).introspect()
        fields = state.tables["users"].fields

        assert "following" not in fields
        assert "name" in fields


class TestNullableOnOperations:
    """Defect 3 — AddField / AlterField must be able to emit ``option<T>``."""

    def test_add_field_wraps_nullable_type(self) -> None:
        """``nullable=True`` produces an optional column."""
        from src.surreal_orm.migrations.operations import AddField

        op = AddField(table="users", name="nickname", field_type="string", nullable=True)

        assert "TYPE option<string>" in op.forwards()

    def test_add_field_leaves_non_nullable_type_alone(self) -> None:
        """The default stays non-optional, as hand-written migrations expect."""
        from src.surreal_orm.migrations.operations import AddField

        op = AddField(table="users", name="email", field_type="string")

        assert "TYPE string" in op.forwards()
        assert "option<" not in op.forwards()

    def test_add_field_does_not_double_wrap(self) -> None:
        """An already-optional type is not wrapped a second time."""
        from src.surreal_orm.migrations.operations import AddField

        op = AddField(table="users", name="nickname", field_type="option<string>", nullable=True)

        assert "TYPE option<string>" in op.forwards()
        assert "option<option<" not in op.forwards()

    def test_alter_field_wraps_nullable_type(self) -> None:
        """``AlterField`` honours ``nullable`` the same way."""
        from src.surreal_orm.migrations.operations import AlterField

        op = AlterField(table="users", name="nickname", field_type="string", nullable=True)

        assert "TYPE option<string>" in op.forwards()

    def test_alter_field_rollback_restores_previous_nullability(self) -> None:
        """A rollback must restore the optionality the column had before."""
        from src.surreal_orm.migrations.operations import AlterField

        op = AlterField(
            table="users",
            name="nickname",
            field_type="string",
            nullable=False,
            previous_type="string",
            previous_nullable=True,
        )

        assert "TYPE option<string>" in op.backwards()
        assert "TYPE string" in op.forwards()


class TestDiffCarriesFieldState:
    """Defects 3 and 4 — every diff branch must forward the full FieldState."""

    def _states(self, field: object) -> tuple[object, object]:
        from src.surreal_orm.migrations.state import SchemaState, TableState

        current = SchemaState()
        target = SchemaState()
        target.tables["posts"] = TableState(name="posts", fields={"author": field})
        return current, target

    def test_new_table_branch_carries_nullable(self) -> None:
        """Creating a table keeps its optional columns optional."""
        from src.surreal_orm.migrations.operations import AddField
        from src.surreal_orm.migrations.state import FieldState

        current, target = self._states(FieldState(name="author", field_type="string", nullable=True))
        ops = current.diff(target)  # type: ignore[attr-defined]

        add = next(op for op in ops if isinstance(op, AddField))
        assert "TYPE option<string>" in add.forwards()

    def test_existing_table_branch_carries_reference_and_on_delete(self) -> None:
        """Adding a field to an existing table keeps its REFERENCE clause."""
        from src.surreal_orm.migrations.operations import AddField
        from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState

        current = SchemaState()
        current.tables["posts"] = TableState(name="posts")
        target = SchemaState()
        target.tables["posts"] = TableState(
            name="posts",
            fields={
                "author": FieldState(
                    name="author",
                    field_type="record<users>",
                    nullable=True,
                    reference=True,
                    on_delete="CASCADE",
                )
            },
        )

        ops = current.diff(target)
        add = next(op for op in ops if isinstance(op, AddField))
        sql = add.forwards()

        assert "TYPE option<record<users>>" in sql
        assert "REFERENCE ON DELETE CASCADE" in sql

    def test_alter_branch_carries_nullable_and_previous_nullable(self) -> None:
        """A field turning optional produces an optional ALTER and a rollback."""
        from src.surreal_orm.migrations.operations import AlterField
        from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState

        current = SchemaState()
        current.tables["users"] = TableState(
            name="users",
            fields={"nickname": FieldState(name="nickname", field_type="string", nullable=False)},
        )
        target = SchemaState()
        target.tables["users"] = TableState(
            name="users",
            fields={"nickname": FieldState(name="nickname", field_type="string", nullable=True)},
        )

        ops = current.diff(target)
        alter = next(op for op in ops if isinstance(op, AlterField))

        assert "TYPE option<string>" in alter.forwards()
        assert "TYPE string" in alter.backwards()
        assert "option<" not in alter.backwards()
