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

from src.surreal_orm.migrations.define_parser import parse_define_field
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


class TestFieldStatePermissions:
    """Models cannot declare field permissions, so they must not make a field differ."""

    def test_default_is_empty(self) -> None:
        assert FieldState(name="a", field_type="string").permissions == {}

    def test_equality_ignores_permissions(self) -> None:
        database = FieldState(name="a", field_type="string", permissions={"select": "NONE"})
        model = FieldState(name="a", field_type="string")

        assert database == model
