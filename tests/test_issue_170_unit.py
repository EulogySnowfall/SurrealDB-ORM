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

from typing import get_args

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
        """``on_delete`` reaches ``FieldState`` in SurrealDB's vocabulary.

        Django's ``SET_NULL`` is stored as ``UNSET`` — the spelling the database
        reports back — so the state compares equal on the next diff.
        """

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
        assert field.on_delete == "UNSET"

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


class TestOnDeleteNormalization:
    """Review: the model side stored Django's vocabulary, the database reports
    SurrealDB's, and ``FieldState.__eq__`` compares raw strings — so every
    non-CASCADE strategy diffed forever without ever converging."""

    def test_field_state_stores_the_surrealdb_keyword(self) -> None:
        """``SET_NULL`` reaches ``FieldState`` as SurrealDB's ``UNSET``."""

        class NormUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None

        class NormPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("NormUser", on_delete="SET_NULL")

        state = ModelIntrospector([NormPost]).introspect()

        assert state.tables["posts"].fields["author"].on_delete == "UNSET"

    def test_emitted_ddl_parses_back_to_an_equal_field_state(self) -> None:
        """The producer and the parser agree, so the diff converges."""
        from src.surreal_orm.migrations.define_parser import parse_define_field
        from src.surreal_orm.migrations.operations import AddField

        class RoundUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None

        class RoundPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("RoundUser", on_delete="PROTECT")

        state = ModelIntrospector([RoundPost]).introspect()
        field = state.tables["posts"].fields["author"]
        ddl = AddField.from_field_state("posts", field).forwards()

        assert parse_define_field(ddl) == field


class TestReferencesTargetResolution:
    """Review: the ReferencesField branch used the marker verbatim — and
    lowercased — while the ForeignKey branch resolved through the registry."""

    def test_references_field_resolves_a_model_name_to_its_table(self) -> None:
        """A model name reaches DDL as the model's configured table."""

        class CamelWriter(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="CamelWriters")
            id: str | None = None

        class CamelShelf(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="shelves")
            id: str | None = None
            writers: ReferencesField["CamelWriter"]

        state = ModelIntrospector([CamelShelf]).introspect()

        assert state.tables["shelves"].fields["writers"].field_type == "array<record<CamelWriters>>"

    def test_references_field_keeps_an_unresolved_table_name(self) -> None:
        """A bare table name is documented usage and must survive verbatim."""

        class LooseShelf(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="shelves")
            id: str | None = None
            books: ReferencesField["books"]  # noqa: F821

        state = ModelIntrospector([LooseShelf]).introspect()

        assert state.tables["shelves"].fields["books"].field_type == "array<record<books>>"


class TestUnionWrappedMarker:
    """Review: `ForeignKey("X") | None` made get_origin() a Union, so both
    marker lookups missed and the field regressed to `any`."""

    def test_foreign_key_wrapped_in_an_optional_union_is_still_a_record_link(self) -> None:
        """The redundant outer ``| None`` does not hide the marker."""

        class OptUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None

        class OptPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("OptUser") | None = None

        state = ModelIntrospector([OptPost]).introspect()
        field = state.tables["posts"].fields["author"]

        assert field.field_type == "record<users>"
        assert field.reference is True


class TestNullableUnionGuard:
    """Review: `"|" in field_type` treated every union as already optional."""

    def test_a_union_without_none_is_still_wrapped(self) -> None:
        """``int | string`` is not optional, so ``nullable`` must apply."""
        from src.surreal_orm.migrations.operations import AddField

        op = AddField(table="t", name="f", field_type="int | string", nullable=True)

        assert "TYPE option<int | string>" in op.forwards()

    def test_a_union_carrying_none_is_left_alone(self) -> None:
        """SurrealDB 3.x reports optional unions as ``none | T``."""
        from src.surreal_orm.migrations.operations import AddField

        op = AddField(table="t", name="f", field_type="none | int", nullable=True)

        assert "TYPE none | int" in op.forwards()
        assert "option<" not in op.forwards()


class TestAlterFieldRollbackCompleteness:
    """Review: backwards() silently dropped REFERENCE, FLEXIBLE and READONLY."""

    def test_rollback_restores_the_reference_clause(self) -> None:
        """Rolling back an FK alter must not disable referential integrity."""
        from src.surreal_orm.migrations.operations import AlterField

        op = AlterField(
            table="posts",
            name="author",
            field_type="record<users>",
            reference=True,
            on_delete="CASCADE",
            previous_type="record<users>",
            previous_nullable=True,
            previous_reference=True,
            previous_on_delete="UNSET",
        )

        assert "REFERENCE ON DELETE UNSET" in op.backwards()

    def test_diff_forwards_every_previous_attribute(self) -> None:
        """The alter branch must carry the whole previous FieldState."""
        from src.surreal_orm.migrations.operations import AlterField
        from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState

        current = SchemaState()
        current.tables["posts"] = TableState(
            name="posts",
            fields={
                "author": FieldState(
                    name="author",
                    field_type="record<users>",
                    nullable=True,
                    flexible=True,
                    readonly=True,
                    reference=True,
                    on_delete="UNSET",
                )
            },
        )
        target = SchemaState()
        target.tables["posts"] = TableState(
            name="posts",
            fields={"author": FieldState(name="author", field_type="record<users>", nullable=True)},
        )

        alter = next(op for op in current.diff(target) if isinstance(op, AlterField))
        rollback = alter.backwards()

        assert "FLEXIBLE" in rollback
        assert "READONLY" in rollback
        assert "REFERENCE ON DELETE UNSET" in rollback


class TestOnDeleteVocabulary:
    """Review: ``THEN`` requires an expression the ORM has no channel for."""

    def test_then_is_not_an_accepted_strategy(self) -> None:
        """``ON DELETE THEN`` without an expression is a parse error."""
        from src.surreal_orm.fields.relation import OnDelete

        assert "THEN" not in get_args(OnDelete)


class TestModelGeneratorRecordLinks:
    """Review: inspectdb mapped every reference field to ReferencesField, so a
    scalar record link round-tripped into a destructive scalar→array alter."""

    def test_scalar_record_reference_generates_a_foreign_key(self) -> None:
        """A scalar ``record<T> REFERENCE`` is a ForeignKey, not a list."""
        from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
        from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState

        state = SchemaState()
        state.tables["posts"] = TableState(
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

        code = ModelCodeGenerator().generate(state)

        assert 'author: ForeignKey("users", on_delete="CASCADE")' in code
        assert "ReferencesField" not in code

    def test_array_record_reference_still_generates_references_field(self) -> None:
        """The plural form keeps its ReferencesField mapping."""
        from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
        from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState

        state = SchemaState()
        state.tables["authors"] = TableState(
            name="authors",
            fields={
                "books": FieldState(
                    name="books",
                    field_type="array<record<books>>",
                    nullable=True,
                    reference=True,
                    on_delete="CASCADE",
                )
            },
        )

        code = ModelCodeGenerator().generate(state)

        assert 'books: ReferencesField["books", "CASCADE"]' in code

    def test_generated_foreign_key_round_trips_without_a_type_change(self) -> None:
        """Re-introspecting the generated model reproduces the same column."""
        from src.surreal_orm.migrations.state import FieldState

        class RtUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="users")
            id: str | None = None

        class RtPost(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="posts")
            id: str | None = None
            author: ForeignKey("RtUser", on_delete="CASCADE")

        from_db = FieldState(
            name="author",
            field_type="record<users>",
            nullable=True,
            reference=True,
            on_delete="CASCADE",
        )
        from_model = ModelIntrospector([RtPost]).introspect().tables["posts"].fields["author"]

        assert from_model == from_db
