"""
Unit tests for the #179 backport onto the v2 (SurrealDB 2.6.x) line.

Two of the four defects from the original issue apply here. Both were reproduced
against this branch before porting:

  colonnes introspectées : ['name', 'nickname', 'groups', 'follows']
  DEFINE FIELD nickname ON probe_users TYPE string;   <- optionality lost
  DEFINE FIELD groups ON probe_users TYPE any;        <- virtual field as a column
  DEFINE FIELD follows ON probe_users TYPE any;

The ``REFERENCE`` / ``ON DELETE`` half is deliberately **not** backported: record
references are a SurrealDB 3.0 feature and ``ReferencesField`` does not exist on
this branch. The ``ForeignKey`` -> ``record<T>`` typing depends on
``_resolve_target_table``, which arrives with the #174 backport.
"""

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.fields import ManyToMany, Relation
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.migrations.operations import AddField, AlterField
from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState


class TestVirtualFieldsExcluded:
    """Graph edges live in their own tables, so they define no column."""

    def test_many_to_many_is_not_a_column(self) -> None:
        """A ManyToMany field never reaches the table state."""

        class M2mUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="m2m_users")
            id: str | None = None
            name: str
            groups: ManyToMany("M2mUser")

        fields = ModelIntrospector([M2mUser])._introspect_model(M2mUser).fields

        assert "groups" not in fields
        assert "name" in fields

    def test_graph_relation_is_not_a_column(self) -> None:
        """A Relation field never reaches the table state."""

        class RelUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="rel_users")
            id: str | None = None
            name: str
            follows: Relation("f", "RelUser")

        fields = ModelIntrospector([RelUser])._introspect_model(RelUser).fields

        assert "follows" not in fields
        assert "name" in fields


class TestNullableReachesTheDdl:
    """``FieldState.nullable`` stopped at the operation boundary."""

    def test_add_field_wraps_a_nullable_type(self) -> None:
        """``nullable=True`` produces an optional column."""
        assert "TYPE option<string>" in AddField(table="t", name="f", field_type="string", nullable=True).forwards()

    def test_add_field_leaves_a_required_type_alone(self) -> None:
        """The default stays non-optional, as hand-written migrations expect."""
        sql = AddField(table="t", name="f", field_type="string").forwards()

        assert "TYPE string" in sql
        assert "option<" not in sql

    def test_add_field_does_not_double_wrap(self) -> None:
        """An already-optional type is not wrapped twice."""
        sql = AddField(table="t", name="f", field_type="option<string>", nullable=True).forwards()

        assert "option<option<" not in sql

    def test_a_union_without_none_is_still_wrapped(self) -> None:
        """Only a union carrying ``none``/``null`` is already optional."""
        assert "TYPE option<int | string>" in AddField(table="t", name="f", field_type="int | string", nullable=True).forwards()

    def test_alter_field_rollback_restores_previous_nullability(self) -> None:
        """A rollback must restore the optionality the column actually had."""
        op = AlterField(
            table="t",
            name="f",
            field_type="string",
            nullable=False,
            previous_type="string",
            previous_nullable=True,
        )

        assert "TYPE option<string>" in op.backwards()
        assert "TYPE string" in op.forwards()

    def test_an_optional_model_field_reaches_the_ddl_as_optional(self) -> None:
        """End to end: the model says optional, the DDL says option<>."""

        class OptModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="opt_models")
            id: str | None = None
            nickname: str | None = None

        state = ModelIntrospector([OptModel]).introspect()
        add = next(op for op in SchemaState().diff(state) if isinstance(op, AddField))

        assert "TYPE option<string>" in add.forwards()


class TestOperationsAreBuiltInOnePlace:
    """The four hand-copied kwarg lists are what let attributes go missing."""

    def test_add_field_is_built_from_a_field_state(self) -> None:
        """``from_field_state`` carries the whole state, nullability included."""
        state = FieldState(name="f", field_type="string", nullable=True, readonly=True)

        sql = AddField.from_field_state("t", state).forwards()

        assert "TYPE option<string>" in sql
        assert "READONLY" in sql

    def test_alter_field_carries_both_states(self) -> None:
        """``from_field_states`` also wires the previous_* attributes."""
        current = FieldState(name="f", field_type="string", nullable=True, flexible=True, readonly=True)
        target = FieldState(name="f", field_type="int", nullable=False)

        op = AlterField.from_field_states("t", current, target)
        rollback = op.backwards()

        assert "TYPE int" in op.forwards()
        assert "TYPE option<string>" in rollback
        assert "FLEXIBLE" in rollback
        assert "READONLY" in rollback

    def test_the_diff_forwards_every_previous_attribute(self) -> None:
        """A real diff must produce a rollback that restores everything."""
        current = SchemaState()
        current.tables["t"] = TableState(
            name="t",
            fields={"f": FieldState(name="f", field_type="string", nullable=True, flexible=True, readonly=True)},
        )
        target = SchemaState()
        target.tables["t"] = TableState(name="t", fields={"f": FieldState(name="f", field_type="int", nullable=False)})

        alter = next(op for op in current.diff(target) if isinstance(op, AlterField))
        rollback = alter.backwards()

        assert "TYPE option<string>" in rollback
        assert "FLEXIBLE" in rollback
        assert "READONLY" in rollback
