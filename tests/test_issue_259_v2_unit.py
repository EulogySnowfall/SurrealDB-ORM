"""
Unit tests for #259 on the v2 (SurrealDB 2.x) line: field-level PERMISSIONS.

``AlterField`` redefines a field with ``DEFINE FIELD OVERWRITE``, which replaces
the whole definition, while the ORM only re-emitted the clauses it models.
Field permissions were not modelled, so a permission set outside the ORM fell
back to the server default, ``PERMISSIONS FULL``, on the next alter or rollback.

The parser made it worse: it treated ``PERMISSIONS`` as one more clause keyword,
so a condition naming ``type`` became the field type, and the ``>`` of
``ASSERT $value > 0`` was read as a closing angle bracket, swallowing the whole
permissions clause into the assertion. Both shapes below are reported verbatim
by ``INFO FOR TABLE`` on SurrealDB 2.7.0.
"""

from pathlib import Path

import pytest

from src.surreal_orm.migrations.define_parser import parse_define_field
from src.surreal_orm.migrations.generator import MigrationGenerator
from src.surreal_orm.migrations.operations import (
    AddField,
    AlterField,
    _effective_field_permissions,
    _effective_permissions,
)
from src.surreal_orm.migrations.state import FieldState


class TestParseFieldPermissions:
    """``parse_define_field()`` cuts the PERMISSIONS clause off before the others."""

    def test_condition_naming_type_does_not_become_the_field_type(self) -> None:
        result = parse_define_field(
            "DEFINE FIELD title ON t TYPE string PERMISSIONS FOR select WHERE type = 'public', FOR create, update FULL"
        )

        assert result.field_type == "string"
        assert result.permissions == {"select": "type = 'public'"}

    def test_comparison_in_assert_does_not_swallow_the_permissions(self) -> None:
        result = parse_define_field(
            "DEFINE FIELD n ON t TYPE int ASSERT $value > 0 PERMISSIONS FOR select NONE, FOR create, update FULL"
        )

        assert result.field_type == "int"
        assert result.assertion == "$value > 0"
        assert result.permissions == {"select": "NONE"}

    def test_the_field_default_normalises_to_empty(self) -> None:
        """``PERMISSIONS FULL`` is what the server reports for a field with no clause."""
        result = parse_define_field("DEFINE FIELD email ON users TYPE string PERMISSIONS FULL")

        assert result.field_type == "string"
        assert result.permissions == {}

    def test_permissions_none_is_kept(self) -> None:
        result = parse_define_field("DEFINE FIELD secret ON users TYPE string PERMISSIONS NONE")

        assert result.permissions == {"select": "NONE", "create": "NONE", "update": "NONE"}

    def test_grouped_none_is_kept(self) -> None:
        """Separate NONE groups are reported back grouped."""
        result = parse_define_field("DEFINE FIELD secret ON users TYPE string PERMISSIONS FOR select, create, update NONE")

        assert result.permissions == {"select": "NONE", "create": "NONE", "update": "NONE"}

    def test_full_entries_are_dropped_and_restrictions_kept(self) -> None:
        result = parse_define_field(
            "DEFINE FIELD email ON users TYPE string "
            "PERMISSIONS FOR select WHERE $auth.id = id, FOR create NONE, FOR update FULL"
        )

        assert result.permissions == {"select": "$auth.id = id", "create": "NONE"}

    def test_permissions_keyword_inside_a_string_is_not_the_clause(self) -> None:
        result = parse_define_field("DEFINE FIELD note ON t TYPE string DEFAULT 'PERMISSIONS FOR all' PERMISSIONS FULL")

        assert result.default == "PERMISSIONS FOR all"
        assert result.permissions == {}

    def test_every_other_clause_survives_ahead_of_the_permissions(self) -> None:
        """The full clause order, verbatim from ``INFO FOR TABLE`` on 2.7.0."""
        result = parse_define_field(
            "DEFINE FIELD s ON p259 FLEXIBLE TYPE option<object> DEFAULT {  } READONLY VALUE $value "
            "ASSERT $value != NONE COMMENT 'a > b' PERMISSIONS FOR select WHERE `value` > 0, FOR create, update FULL"
        )

        assert result.field_type == "object"
        assert result.nullable is True
        assert result.flexible is True
        assert result.readonly is True
        assert result.value == "$value"
        assert result.assertion == "$value != NONE"
        assert result.comment == "a > b"
        assert result.permissions == {"select": "`value` > 0"}

    def test_a_full_group_reported_first_is_dropped(self) -> None:
        """The server groups by rule, so a FULL group can come before a condition."""
        result = parse_define_field(
            "DEFINE FIELD e ON t TYPE int PERMISSIONS FOR select, update FULL, FOR create WHERE $auth.id = id"
        )

        assert result.permissions == {"create": "$auth.id = id"}

    def test_no_permissions_clause(self) -> None:
        result = parse_define_field("DEFINE FIELD email ON users TYPE string")

        assert result.permissions == {}

    def test_an_expression_naming_a_permissions_field_is_not_cut(self) -> None:
        """``permissions`` is no longer a clause keyword: only the clause is."""
        result = parse_define_field(
            "DEFINE FIELD p ON p259 TYPE string VALUE permissions ?? 'x' PERMISSIONS FOR select NONE, FOR create, update FULL"
        )

        assert result.value == "permissions ?? 'x'"
        assert result.permissions == {"select": "NONE"}


class TestEffectiveFieldPermissions:
    """A field's default is FULL, the opposite of a table's (NONE)."""

    def test_full_is_dropped_none_and_conditions_are_kept(self) -> None:
        assert _effective_field_permissions({"select": "NONE", "create": "FULL", "update": "$auth.id = id"}) == {
            "select": "NONE",
            "update": "$auth.id = id",
        }

    def test_delete_is_dropped(self) -> None:
        """Accepted on a field, ignored and never reported by the server."""
        assert _effective_field_permissions({"select": "NONE", "delete": "NONE"}) == {"select": "NONE"}

    def test_empty_and_none(self) -> None:
        assert _effective_field_permissions(None) == {}
        assert _effective_field_permissions({}) == {}

    def test_the_table_helper_would_drop_the_restriction(self) -> None:
        """Why the table helper is not reused: it drops NONE as the default."""
        assert _effective_permissions({"select": "NONE"}) == {}
        assert _effective_field_permissions({"select": "NONE"}) == {"select": "NONE"}


class TestFieldStatePermissions:
    """Models cannot declare field permissions, so they must not make a field differ."""

    def test_default_is_empty(self) -> None:
        assert FieldState(name="a", field_type="string").permissions == {}

    def test_equality_ignores_permissions(self) -> None:
        database = FieldState(name="a", field_type="string", permissions={"select": "NONE"})
        model = FieldState(name="a", field_type="string")

        assert database == model
        assert not database.has_changed(model)


def _alter(current: FieldState) -> AlterField:
    """The AlterField the diff builds when a model changes *current*'s assertion."""
    target = FieldState(name=current.name, field_type=current.field_type, nullable=False, assertion="$value != NONE")
    return AlterField.from_field_states("t", current, target)


class TestAlterFieldCarriesThePermissions:
    """``DEFINE FIELD OVERWRITE`` replaces the whole definition, PERMISSIONS included."""

    def test_from_field_states_fills_both_slots_from_the_current_state(self) -> None:
        current = FieldState(name="n", field_type="int", nullable=False, permissions={"select": "NONE"})

        op = _alter(current)

        assert op.permissions == {"select": "NONE"}
        assert op.previous_permissions == {"select": "NONE"}
        assert op.permissions is not op.previous_permissions
        assert op.permissions is not current.permissions

    def test_from_field_states_normalises_the_current_state(self) -> None:
        current = FieldState(name="n", field_type="int", nullable=False, permissions={"select": "NONE", "update": "FULL"})

        assert _alter(current).permissions == {"select": "NONE"}

    def test_default_permissions_leave_both_slots_none(self) -> None:
        op = _alter(FieldState(name="n", field_type="int", nullable=False))

        assert op.permissions is None
        assert op.previous_permissions is None

    def test_both_directions_emit_the_clause_last(self) -> None:
        current = FieldState(
            name="n",
            field_type="int",
            nullable=False,
            assertion="$value > 0",
            comment="count",
            permissions={"select": "NONE", "update": "$auth.id = id"},
        )

        op = _alter(current)

        assert op.forwards() == (
            "DEFINE FIELD OVERWRITE n ON t TYPE int ASSERT $value != NONE "
            "PERMISSIONS FOR select NONE FOR update WHERE $auth.id = id;"
        )
        assert op.backwards() == (
            "DEFINE FIELD OVERWRITE n ON t TYPE int ASSERT $value > 0 COMMENT 'count' "
            "PERMISSIONS FOR select NONE FOR update WHERE $auth.id = id;"
        )

    def test_default_permissions_emit_no_clause(self) -> None:
        """Output is unchanged for a field at the server default."""
        op = _alter(FieldState(name="n", field_type="int", nullable=False, assertion="$value > 0"))

        assert op.forwards() == "DEFINE FIELD OVERWRITE n ON t TYPE int ASSERT $value != NONE;"
        assert op.backwards() == "DEFINE FIELD OVERWRITE n ON t TYPE int ASSERT $value > 0;"

    def test_hand_written_alter_field_is_unchanged(self) -> None:
        op = AlterField(table="t", name="n", field_type="int", previous_type="string")

        assert op.forwards() == "DEFINE FIELD OVERWRITE n ON t TYPE int;"
        assert op.backwards() == "DEFINE FIELD OVERWRITE n ON t TYPE string;"

    def test_add_field_never_emits_permissions(self) -> None:
        state = FieldState(name="n", field_type="int", nullable=False, permissions={"select": "NONE"})

        assert AddField.from_field_state("t", state).forwards() == "DEFINE FIELD n ON t TYPE int;"


class TestRealShapesAreReproduced:
    """Shapes reported verbatim by ``INFO FOR TABLE`` on 2.7.0, through parse → alter.

    The rendered clause must reproduce each condition byte for byte. The
    integration test checks that the server then reports the same clause back.
    """

    @pytest.mark.parametrize(
        ("statement", "clause"),
        [
            pytest.param(
                "DEFINE FIELD a ON p259 TYPE string PERMISSIONS FOR select WHERE "
                "string::contains($value, 'x, y') AND type = \"it's\", FOR create, update FULL",
                # The group splitter tracks single quotes only, so the
                # apostrophe inside "it's" keeps the trailing FULL group
                # attached to the condition — and the text comes out unchanged.
                "PERMISSIONS FOR select WHERE string::contains($value, 'x, y') AND type = \"it's\", FOR create, update FULL",
                id="comma-in-call-and-double-quoted-apostrophe",
            ),
            pytest.param(
                "DEFINE FIELD b ON p259 TYPE int PERMISSIONS FOR select, create FULL, FOR update WHERE `value` > 0",
                "PERMISSIONS FOR update WHERE `value` > 0",
                id="full-group-first-and-backticked-value",
            ),
            pytest.param(
                "DEFINE FIELD c ON p259 TYPE string "
                "PERMISSIONS FOR select WHERE $auth.id = id, FOR create NONE, FOR update FULL",
                "PERMISSIONS FOR select WHERE $auth.id = id FOR create NONE",
                id="condition-none-and-full",
            ),
        ],
    )
    def test_the_condition_comes_out_identical(self, statement: str, clause: str) -> None:
        current = parse_define_field(statement)

        op = _alter(current)

        assert op.forwards().endswith(f" {clause};")
        assert op.backwards().endswith(f" {clause};")


class TestGeneratedMigrationFile:
    """Generated files must spell both slots out, or the fix stops at the diff."""

    @staticmethod
    def _rebuild(rendered: str) -> AlterField:
        # Evaluated with AlterField as the only name, as a migration file would build it.
        rebuilt = eval(rendered.strip().rstrip(","), {"AlterField": AlterField})
        assert isinstance(rebuilt, AlterField)
        return rebuilt

    @pytest.mark.parametrize(
        "permissions",
        [
            {"select": "NONE"},
            {"select": "type = 'public'", "create": "NONE"},
            {"select": "NONE", "create": "NONE", "update": "$auth.id = id"},
        ],
    )
    def test_both_slots_round_trip(self, tmp_path: Path, permissions: dict[str, str]) -> None:
        current = FieldState(name="n", field_type="int", nullable=False, permissions=permissions)
        op = _alter(current)
        generator = MigrationGenerator(tmp_path)

        rendered = generator._render_operation(op)
        rebuilt = self._rebuild(rendered)

        assert "permissions=" in rendered
        assert "previous_permissions=" in rendered
        assert rebuilt == op
        assert rebuilt.forwards() == op.forwards()
        assert rebuilt.backwards() == op.backwards()

    def test_default_permissions_are_not_written(self, tmp_path: Path) -> None:
        op = _alter(FieldState(name="n", field_type="int", nullable=False))

        rendered = MigrationGenerator(tmp_path)._render_operation(op)

        assert "permissions" not in rendered
        assert self._rebuild(rendered) == op

    def test_the_generated_file_compiles(self, tmp_path: Path) -> None:
        op = _alter(FieldState(name="n", field_type="int", nullable=False, permissions={"select": "NONE"}))

        path = MigrationGenerator(tmp_path).generate("alter_n", [op])
        content = path.read_text()

        compile(content, str(path), "exec")
        assert "permissions={'select': 'NONE'}" in content
        assert "previous_permissions={'select': 'NONE'}" in content
