"""
Unit tests for issue #195 — altering a field must not reset what the ORM does not model.

``AlterField`` emitted ``DEFINE FIELD OVERWRITE``, which replaces the *whole*
definition. A field-level ``PERMISSIONS`` clause is not part of ``FieldState``,
so it was never re-emitted: SurrealDB fell back to the field default,
``PERMISSIONS FULL``, and a restriction set outside the ORM was gone after the
next alteration — forwards, rollback and ``define_table()`` alike.

``ALTER FIELD`` changes only the clauses it names. Every modelled clause is
named, set when the state has it and ``DROP``-ped when it does not, so the
modelled semantics stay those of a full redefinition while everything else —
``PERMISSIONS`` first — is left as the database has it.

The live-server half is ``tests/test_issue_195_integration.py``.
"""

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.fields import Encrypted
from src.surreal_orm.migrations import operations
from src.surreal_orm.migrations.define_parser import parse_define_field
from src.surreal_orm.migrations.operations import (
    FIELD_DEFINITION_FIELDS,
    AddField,
    AlterField,
)
from src.surreal_orm.migrations.state import FieldState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.types import TableType

#: Every DROP form, in the order the renderer emits them.
ALL_DROPS = (
    "DROP TYPE",
    "DROP FLEXIBLE",
    "DROP VALUE",
    "DROP DEFAULT",
    "DROP ASSERT",
    "DROP READONLY",
    "DROP REFERENCE",
    "DROP COMMENT",
)


class TestForwardsIsAnAlterStatement:
    """``forwards()`` names the field and its clauses, never redefines it."""

    def test_forwards_alters_rather_than_overwrites(self) -> None:
        """OVERWRITE replaced the definition and with it the field's PERMISSIONS."""
        sql = AlterField(table="docs", name="secret", field_type="string", previous_type="string").forwards()

        assert sql.startswith("ALTER FIELD secret ON docs ")
        assert "DEFINE FIELD" not in sql
        assert "OVERWRITE" not in sql

    def test_forwards_never_mentions_permissions(self) -> None:
        """Left untouched by construction: the statement does not name the clause."""
        op = AlterField.from_field_states(
            "docs",
            FieldState(name="secret", field_type="string", nullable=False),
            FieldState(name="secret", field_type="string", nullable=False, assertion="$value != ''"),
        )

        assert "PERMISSIONS" not in op.forwards()
        assert "PERMISSIONS" not in op.backwards()

    def test_a_bare_alter_drops_every_modelled_clause(self) -> None:
        """Nothing set means nothing kept — what OVERWRITE with no clause meant for these."""
        sql = AlterField(table="t", name="f").forwards()

        assert sql == "ALTER FIELD f ON t " + " ".join(ALL_DROPS) + ";"

    def test_a_full_state_sets_every_modelled_clause(self) -> None:
        """Several clauses in one statement, in the tuple's order."""
        sql = AlterField(
            table="posts",
            name="author",
            field_type="record<users>",
            nullable=True,
            flexible=False,
            value="$value",
            default="users:root",
            assertion="$value != NONE",
            readonly=True,
            reference=True,
            on_delete="SET_NULL",
            comment="who wrote it",
        ).forwards()

        assert sql == (
            "ALTER FIELD author ON posts TYPE option<record<users>> DROP FLEXIBLE VALUE $value "
            "DEFAULT 'users:root' ASSERT $value != NONE READONLY REFERENCE ON DELETE UNSET "
            "COMMENT 'who wrote it';"
        )


class TestEachClauseIsSetOrDropped:
    """Removing a clause from the model still removes it from the database."""

    @pytest.mark.parametrize(
        ("attributes", "present", "absent"),
        [
            ({"field_type": "int"}, "TYPE int", "DROP TYPE"),
            ({"field_type": None}, "DROP TYPE", "TYPE int"),
            ({"field_type": "int", "nullable": True}, "TYPE option<int>", "DROP TYPE"),
            ({"field_type": "none | int", "nullable": True}, "TYPE none | int", "option<"),
            ({"field_type": "object", "flexible": True}, "TYPE object FLEXIBLE", "DROP FLEXIBLE"),
            ({"flexible": False}, "DROP FLEXIBLE", "TYPE FLEXIBLE"),
            ({"value": "string::lowercase($value)"}, "VALUE string::lowercase($value)", "DROP VALUE"),
            ({"value": None}, "DROP VALUE", "VALUE string"),
            ({"encrypted": True}, "VALUE crypto::argon2::generate($value)", "DROP VALUE"),
            ({"default": "time::now()"}, "DEFAULT time::now()", "DROP DEFAULT"),
            ({"default": False}, "DEFAULT false", "DROP DEFAULT"),
            ({"default": 0}, "DEFAULT 0", "DROP DEFAULT"),
            ({"default": None}, "DROP DEFAULT", "DEFAULT None"),
            ({"assertion": "$value > 0"}, "ASSERT $value > 0", "DROP ASSERT"),
            ({"assertion": None}, "DROP ASSERT", "ASSERT None"),
            ({"readonly": True}, " READONLY", "DROP READONLY"),
            ({"readonly": False}, "DROP READONLY", "DROP ASSERT READONLY"),
            ({"reference": True, "on_delete": "PROTECT"}, "REFERENCE ON DELETE REJECT", "DROP REFERENCE"),
            ({"reference": True}, "DROP READONLY REFERENCE DROP COMMENT", "ON DELETE"),
            ({"reference": False, "on_delete": "CASCADE"}, "DROP REFERENCE", "ON DELETE"),
            ({"comment": "why"}, "COMMENT 'why'", "DROP COMMENT"),
            ({"comment": None}, "DROP COMMENT", "COMMENT None"),
        ],
    )
    def test_forwards(self, attributes: dict[str, Any], present: str, absent: str) -> None:
        """Each modelled clause renders as its value, or as its DROP form."""
        sql = AlterField(table="t", name="f", **attributes).forwards()

        assert present in sql
        assert absent not in sql

    @pytest.mark.parametrize(
        ("attributes", "present", "absent"),
        [
            ({"previous_type": "int"}, "TYPE int", "DROP TYPE"),
            ({"previous_type": "int", "previous_nullable": True}, "TYPE option<int>", "DROP TYPE"),
            ({"previous_type": "object", "previous_flexible": True}, "TYPE object FLEXIBLE", "DROP FLEXIBLE"),
            ({"previous_type": "int"}, "DROP FLEXIBLE", "int FLEXIBLE"),
            ({"previous_type": "int", "previous_value": "$value * 2"}, "VALUE $value * 2", "DROP VALUE"),
            ({"previous_type": "int"}, "DROP VALUE", "VALUE $"),
            ({"previous_type": "int", "previous_default": 3}, "DEFAULT 3", "DROP DEFAULT"),
            ({"previous_type": "int"}, "DROP DEFAULT", "DEFAULT 3"),
            ({"previous_type": "int", "previous_assertion": "$value > 0"}, "ASSERT $value > 0", "DROP ASSERT"),
            ({"previous_type": "int"}, "DROP ASSERT", "ASSERT $"),
            ({"previous_type": "int", "previous_readonly": True}, " READONLY", "DROP READONLY"),
            ({"previous_type": "int"}, "DROP READONLY", "DROP ASSERT READONLY"),
            (
                {"previous_type": "record<users>", "previous_reference": True, "previous_on_delete": "UNSET"},
                "REFERENCE ON DELETE UNSET",
                "DROP REFERENCE",
            ),
            ({"previous_type": "int"}, "DROP REFERENCE", "ON DELETE"),
            ({"previous_type": "int", "previous_comment": "why"}, "COMMENT 'why'", "DROP COMMENT"),
            ({"previous_type": "int"}, "DROP COMMENT", "COMMENT '"),
        ],
    )
    def test_backwards(self, attributes: dict[str, Any], present: str, absent: str) -> None:
        """The rollback restores each previous clause, or drops it when there was none."""
        sql = AlterField(table="t", name="f", field_type="string", **attributes).backwards()

        assert sql.startswith("ALTER FIELD f ON t ")
        assert present in sql
        assert absent not in sql

    def test_a_rollback_with_no_previous_type_is_still_empty(self) -> None:
        """Irreversible as before: there is nothing to restore."""
        op = AlterField(table="t", name="f", field_type="int")

        assert op.backwards() == ""
        assert op.reversible is False


class TestTheRollbackRestoresTheReferenceStrategy:
    """``tests/test_alterfield_rollback_unit.py`` holds the rollback invariant; this is its edge.

    ``ALTER FIELD ... REFERENCE`` with no strategy resets ON DELETE to the
    server default, so the strategy must travel with every REFERENCE.
    """

    def test_on_delete_is_carried_with_the_reference(self) -> None:
        """A rollback must not re-enable a reference with the default strategy."""
        previous = FieldState(name="f", field_type="record<users>", reference=True, on_delete="CASCADE")
        target = FieldState(name="f", field_type="record<users>")

        rollback = AlterField.from_field_states("t", previous, target).backwards()

        assert "REFERENCE ON DELETE CASCADE" in rollback


class TestTheRenderersStayInStep:
    """One tuple drives both statement shapes."""

    @pytest.mark.parametrize("renderer", ["_render_define_field", "_render_alter_field"])
    def test_every_definition_field_is_a_renderer_parameter(self, renderer: str) -> None:
        """A clause added to the tuple must reach both statements."""
        parameters = inspect.signature(getattr(operations, renderer)).parameters
        keyword_only = {name for name, p in parameters.items() if p.kind is p.KEYWORD_ONLY}

        assert set(FIELD_DEFINITION_FIELDS) <= keyword_only, renderer

    def test_alter_renders_nothing_the_tuple_does_not_carry(self) -> None:
        """No mode flag: the ALTER statement has no OVERWRITE/IF NOT EXISTS variant."""
        parameters = inspect.signature(operations._render_alter_field).parameters
        keyword_only = {name for name, p in parameters.items() if p.kind is p.KEYWORD_ONLY}

        assert keyword_only == set(FIELD_DEFINITION_FIELDS)


class TestAddFieldIsUnchanged:
    """A migration adds a field that is not there yet; a second run must fail."""

    def test_add_field_is_a_plain_define(self) -> None:
        sql = AddField(table="users", name="age", field_type="int", default=0).forwards()

        assert sql == "DEFINE FIELD age ON users TYPE int DEFAULT 0;"

    def test_the_overwrite_opt_in_still_works(self) -> None:
        """Kept for compatibility; ``define_table()`` no longer uses it."""
        sql = AddField(table="users", name="age", field_type="int", overwrite=True).forwards()

        assert sql == "DEFINE FIELD OVERWRITE age ON users TYPE int;"


class I195UnitDoc(BaseSurrealModel):
    """A plain table: every field gets the create-or-update pair."""

    model_config = SurrealConfigDict(table_name="i195_unit_docs")

    id: str | None = None
    title: str
    secret: str | None = None


class I195UnitUser(BaseSurrealModel):
    """A USER table: the password is hashed by SIGNUP, not by a VALUE clause."""

    model_config = SurrealConfigDict(table_name="i195_unit_users", table_type=TableType.USER)

    id: str | None = None
    email: str
    password: Encrypted


def _fake_client() -> MagicMock:
    """A client whose query() records the SQL and reports no statement error."""
    client = MagicMock()
    client.query = AsyncMock(return_value=MagicMock(results=[]))
    return client


class TestDefineTableUsesTheCreateOrUpdatePair:
    """``define_table()`` reset every field's permissions with OVERWRITE."""

    async def _define(self, model: type[BaseSurrealModel]) -> str:
        client = _fake_client()
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager.get_client",
            new=AsyncMock(return_value=client),
        ):
            sql = await model.define_table()
        client.query.assert_awaited_once_with(sql)
        return sql

    @pytest.mark.asyncio
    async def test_no_field_is_overwritten(self) -> None:
        sql = await self._define(I195UnitDoc)

        assert "DEFINE FIELD OVERWRITE" not in sql

    @pytest.mark.asyncio
    async def test_each_field_is_created_if_missing_then_altered(self) -> None:
        """Works on an empty database, and keeps unmodelled clauses on an existing one."""
        lines = (await self._define(I195UnitDoc)).splitlines()

        assert lines[0] == "DEFINE TABLE OVERWRITE i195_unit_docs SCHEMAFULL;"
        assert lines[1:] == [
            "DEFINE FIELD IF NOT EXISTS title ON i195_unit_docs TYPE string;",
            "ALTER FIELD title ON i195_unit_docs TYPE string " + " ".join(ALL_DROPS[1:]) + ";",
            "DEFINE FIELD IF NOT EXISTS secret ON i195_unit_docs TYPE option<string>;",
            "ALTER FIELD secret ON i195_unit_docs TYPE option<string> " + " ".join(ALL_DROPS[1:]) + ";",
        ]

    @pytest.mark.asyncio
    async def test_a_user_table_password_still_has_no_value_clause(self) -> None:
        """SIGNUP hashes; a VALUE clause too would hash twice. ALTER drops it."""
        sql = await self._define(I195UnitUser)

        assert "argon2" not in sql
        assert "ALTER FIELD password ON i195_unit_users TYPE string DROP FLEXIBLE DROP VALUE" in sql


class TestTheDefineRendererModes:
    """``IF NOT EXISTS`` and ``OVERWRITE`` are mutually exclusive."""

    def test_if_not_exists(self) -> None:
        sql = operations._render_define_field("t", "f", if_not_exists=True, field_type="int")

        assert sql == "DEFINE FIELD IF NOT EXISTS f ON t TYPE int;"

    def test_both_modes_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="OVERWRITE"):
            operations._render_define_field("t", "f", overwrite=True, if_not_exists=True, field_type="int")


class TestArrayElementSubFieldsFollowTheType:
    """SurrealDB derives ``name.*`` for a typed array or set; ``ALTER FIELD`` does not update it.

    ``DEFINE FIELD tags ON t TYPE array<string>`` also defines ``tags.*`` as
    ``string``, and ``n.*.*`` for ``array<array<…>>``. A redefinition re-derives
    them; ``ALTER FIELD tags ON t TYPE array<int>`` leaves ``tags.*`` at
    ``string``, so the column then refuses every non-empty value. The renderer
    aligns each derived level with ``ALTER FIELD IF EXISTS`` — the sub-field is
    the server's, and whether it exists depends on the previous definition, so
    a missing one is a no-op — naming its TYPE only, so a clause set on it by
    hand survives too.

    Only the levels the server derives are aligned, which is what a redefinition
    did. A typed element is derived always. An ``any`` element, or an untyped
    ``array``/``set``, is derived only under an optional column or a union —
    ``option<array<any>>`` derives ``f.*`` as ``any``, and a column widened from
    ``option<array<int>>`` otherwise kept ``f.*`` at ``int`` and refused every
    other value. Under a non-optional ``array``/``array<any>`` the server derives
    nothing, so a ``tags.*`` defined by hand there must be left alone: forcing it
    to ``any`` dropped its type and accepted values it was written to refuse.
    """

    @pytest.mark.parametrize(
        ("attributes", "statements"),
        [
            ({"field_type": "array<int>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE int;"]),
            ({"field_type": "set<string>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE string;"]),
            ({"field_type": "array<string, 5>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE string;"]),
            (
                {"field_type": "array<array<int>>"},
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE array<int>;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE int;",
                ],
            ),
            (
                {"field_type": "array<set<array<bool>>>"},
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE set<array<bool>>;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE array<bool>;",
                    "ALTER FIELD IF EXISTS f.*.*.* ON t TYPE bool;",
                ],
            ),
            # A ReferencesField: option<array<record<T>>>, the target table in the element.
            (
                {"field_type": "array<record<authors>>", "nullable": True, "reference": True},
                ["ALTER FIELD IF EXISTS f.* ON t TYPE record<authors>;"],
            ),
            ({"field_type": "option<array<int>>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE int;"]),
            ({"field_type": "none | array<int>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE int;"]),
            ({"field_type": "array<option<int>>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE option<int>;"]),
            # The server derives the element from every array/set member of a union.
            ({"field_type": "array<int> | string"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE int;"]),
            ({"field_type": "array<int> | set<string>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE int | string;"]),
            ({"field_type": "array<record<a | b>>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE record<a | b>;"]),
            # An any element, or an untyped array or set, under a non-optional,
            # non-union column: the server derives no level, so none is aligned
            # and a hand-defined one is left as it is.
            ({"field_type": "array"}, []),
            ({"field_type": "array<any>"}, []),
            ({"field_type": "array<any, 5>"}, []),
            ({"field_type": "set"}, []),
            ({"field_type": "set<any>"}, []),
            # The walk stops at the first such level; the typed ones above it stay.
            ({"field_type": "array<array<any>>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE array<any>;"]),
            ({"field_type": "array<array>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE array;"]),
            # Under an optional column the server derives an any element as any:
            # the level is widened, and nothing below it is derived.
            ({"field_type": "array<any>", "nullable": True}, ["ALTER FIELD IF EXISTS f.* ON t TYPE any;"]),
            ({"field_type": "option<array>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE any;"]),
            ({"field_type": "none | array<any>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE any;"]),
            (
                {"field_type": "option<array<array<any>>>"},
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE array<any>;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE any;",
                ],
            ),
            # An optional element counts as well: the union is on the path.
            (
                {"field_type": "array<option<array<any>>>"},
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE option<array<any>>;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE any;",
                ],
            ),
            (
                {"field_type": "array<array<any> | string>"},
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE array<any> | string;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE any;",
                ],
            ),
            # A union with an any element accepts anything at that level.
            ({"field_type": "array<int> | array<any>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE any;"]),
            ({"field_type": "array<any> | array<int>"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE any;"]),
            ({"field_type": "array<int> | set"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE any;"]),
            ({"field_type": "array | string"}, ["ALTER FIELD IF EXISTS f.* ON t TYPE any;"]),
            # Not an array or set: no element level.
            ({"field_type": "any"}, []),
            ({"field_type": "string"}, []),
            ({"field_type": "object", "flexible": True}, []),
            ({"field_type": None}, []),
        ],
    )
    def test_forwards(self, attributes: dict[str, Any], statements: list[str]) -> None:
        """The parent's ALTER first, then one statement per derived level, outermost first."""
        lines = AlterField(table="t", name="f", **attributes).forwards().splitlines()

        assert lines[0].startswith("ALTER FIELD f ON t ")
        assert lines[1:] == statements

    def test_backwards_restores_the_previous_element_type(self) -> None:
        """A database migrated forward by OVERWRITE already has ``tags.*`` in the new type."""
        op = AlterField.from_field_states(
            "t",
            FieldState(name="tags", field_type="array<string>"),
            FieldState(name="tags", field_type="array<int>"),
        )

        assert op.forwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS tags.* ON t TYPE int;"]
        assert op.backwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS tags.* ON t TYPE string;"]

    def test_widening_to_any_and_back(self) -> None:
        """``option<array<int>>`` → ``option<array<any>>``: ``tags.*`` must follow both ways."""
        op = AlterField.from_field_states(
            "t",
            FieldState(name="tags", field_type="array<int>", nullable=True),
            FieldState(name="tags", field_type="array<any>", nullable=True),
        )

        assert op.forwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS tags.* ON t TYPE any;"]
        assert op.backwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS tags.* ON t TYPE int;"]

    def test_narrowing_from_an_untyped_array_and_back(self) -> None:
        """The rollback of a previous untyped ``array`` widens ``tags.*`` back to ``any``."""
        op = AlterField.from_field_states(
            "t",
            FieldState(name="tags", field_type="array", nullable=True),
            FieldState(name="tags", field_type="array<string>", nullable=True),
        )

        assert op.forwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS tags.* ON t TYPE string;"]
        assert op.backwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS tags.* ON t TYPE any;"]

    def test_an_unrelated_change_to_an_untyped_array_leaves_the_element_alone(self) -> None:
        """``array`` → ``array`` with a COMMENT: no ``tags.*`` statement either way.

        The server derived no ``tags.*`` for a non-optional ``array``, so one that
        exists was defined by hand, and an unrelated alteration must not retype it.
        """
        op = AlterField.from_field_states(
            "t",
            FieldState(name="tags", field_type="array", nullable=False),
            FieldState(name="tags", field_type="array", nullable=False, comment="labels"),
        )

        assert op.forwards().splitlines()[1:] == []
        assert op.backwards().splitlines()[1:] == []

    def test_rolling_back_a_narrowing_from_a_non_optional_any_array(self) -> None:
        """``array<any>`` → ``array<string>``: the rollback leaves ``tags.*`` as it is.

        A non-optional ``array<any>`` derives no ``tags.*``; restoring it must not
        turn a ``tags.*`` the forward step typed, or one defined by hand, into
        ``any``.
        """
        op = AlterField.from_field_states(
            "t",
            FieldState(name="tags", field_type="array<any>", nullable=False),
            FieldState(name="tags", field_type="array<string>", nullable=False),
        )

        assert op.forwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS tags.* ON t TYPE string;"]
        assert op.backwards().splitlines()[1:] == []

    def test_a_changed_reference_target_reaches_the_element(self) -> None:
        """``o.*`` kept ``record<old>`` and refused every link to the new table."""
        op = AlterField.from_field_states(
            "t",
            FieldState(name="o", field_type="array<record<old>>", nullable=True, reference=True, on_delete="IGNORE"),
            FieldState(name="o", field_type="array<record<new>>", nullable=True, reference=True, on_delete="IGNORE"),
        )

        assert op.forwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS o.* ON t TYPE record<new>;"]
        assert op.backwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS o.* ON t TYPE record<old>;"]

    def test_the_sub_field_statement_names_its_type_only(self) -> None:
        """No DROP on the derived field: the server owns it, and a hand-set clause there stays."""
        sub = AlterField(table="t", name="f", field_type="array<int>", comment="c").forwards().splitlines()[1]

        assert "DROP" not in sub
        assert "COMMENT" not in sub

    def test_add_field_does_not_need_it(self) -> None:
        """A creation derives the sub-fields itself."""
        assert AddField(table="t", name="f", field_type="array<int>").forwards() == ("DEFINE FIELD f ON t TYPE array<int>;")


class TestFlexibleReachesTheObjectElementLevels:
    """A FLEXIBLE array column made its object elements FLEXIBLE too, under a redefinition.

    ``DEFINE FIELD OVERWRITE items ON t TYPE option<array<object>> FLEXIBLE`` also
    sets ``items.*`` to ``object FLEXIBLE``. Aligning the element levels with
    their TYPE only left ``items.*`` strict: the AlterField that turned FLEXIBLE
    on reported success, and the column still refused ``[{x: 'hello'}]`` with
    ``Found field 'items[0].x', but no such field exists``.

    Of the modelled clauses, only TYPE and FLEXIBLE reach the element levels.
    FLEXIBLE is written on a level whose type contains an object — the parser's
    own rule (``FLEXIBLE can only be used with types containing object``) — and
    never removed from one: a non-FLEXIBLE redefinition left ``items.*``
    FLEXIBLE as well, and a FLEXIBLE set there by hand must survive (#195).
    """

    @pytest.mark.parametrize(
        ("field_type", "statements"),
        [
            ("array<object>", ["ALTER FIELD IF EXISTS f.* ON t TYPE object FLEXIBLE;"]),
            ("option<array<object>>", ["ALTER FIELD IF EXISTS f.* ON t TYPE object FLEXIBLE;"]),
            (
                "option<array<array<object>>>",
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE array<object> FLEXIBLE;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE object FLEXIBLE;",
                ],
            ),
            # The server derives `int FLEXIBLE` for the inner level, but writing
            # FLEXIBLE on `int` is a parse error, and it means nothing there.
            (
                "array<array<int> | object>",
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE array<int> | object FLEXIBLE;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE int;",
                ],
            ),
            ("array<{ a: int }>", ["ALTER FIELD IF EXISTS f.* ON t TYPE { a: int } FLEXIBLE;"]),
            ("array<int> | object", ["ALTER FIELD IF EXISTS f.* ON t TYPE int;"]),
        ],
    )
    def test_forwards(self, field_type: str, statements: list[str]) -> None:
        lines = AlterField(table="t", name="f", field_type=field_type, flexible=True).forwards().splitlines()

        assert " FLEXIBLE " in lines[0]
        assert lines[1:] == statements

    @pytest.mark.parametrize(
        ("field_type", "statements"),
        [
            ("array<object>", ["ALTER FIELD IF EXISTS f.* ON t TYPE object;"]),
            (
                "option<array<array<object>>>",
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE array<object>;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE object;",
                ],
            ),
            (
                "array<array<int> | object>",
                [
                    "ALTER FIELD IF EXISTS f.* ON t TYPE array<int> | object;",
                    "ALTER FIELD IF EXISTS f.*.* ON t TYPE int;",
                ],
            ),
        ],
    )
    def test_backwards_to_a_strict_column(self, field_type: str, statements: list[str]) -> None:
        """The rollback names neither FLEXIBLE nor DROP FLEXIBLE on an element level.

        A non-FLEXIBLE redefinition left ``items.*`` FLEXIBLE, and a FLEXIBLE set
        there by hand must survive: the rollback removes it from the column only.
        """
        op = AlterField.from_field_states(
            "t",
            FieldState(name="f", field_type=field_type, flexible=False),
            FieldState(name="f", field_type=field_type, flexible=True),
        )
        lines = op.backwards().splitlines()

        assert " DROP FLEXIBLE " in lines[0]
        assert lines[1:] == statements
        assert not any("FLEXIBLE" in line for line in lines[1:])

    def test_retyping_a_list_of_strings_to_flexible_objects(self) -> None:
        op = AlterField.from_field_states(
            "t",
            FieldState(name="items", field_type="array<string>"),
            FieldState(name="items", field_type="array<object>", flexible=True),
        )

        assert op.forwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS items.* ON t TYPE object FLEXIBLE;"]
        assert op.backwards().splitlines()[1:] == ["ALTER FIELD IF EXISTS items.* ON t TYPE string;"]

    def test_a_strict_column_leaves_the_element_flexibility_alone(self) -> None:
        """No FLEXIBLE and no DROP FLEXIBLE on ``items.*``: a hand-set FLEXIBLE there stays."""
        sub = AlterField(table="t", name="items", field_type="array<object>").forwards().splitlines()[1]

        assert sub == "ALTER FIELD IF EXISTS items.* ON t TYPE object;"

    def test_the_define_table_pair_carries_it(self) -> None:
        """``define_table()`` renders through the same ALTER, so a FLEXIBLE list column gets it too."""
        sql = operations._render_create_or_alter_field(
            AddField(table="t", name="items", field_type="array<object>", nullable=True, flexible=True)
        )

        assert sql.splitlines() == [
            "DEFINE FIELD IF NOT EXISTS items ON t FLEXIBLE TYPE option<array<object>>;",
            "ALTER FIELD items ON t TYPE option<array<object>> FLEXIBLE " + " ".join(ALL_DROPS[2:]) + ";",
            "ALTER FIELD IF EXISTS items.* ON t TYPE object FLEXIBLE;",
        ]

    @pytest.mark.parametrize(
        ("field_type", "contains"),
        [
            # Accepted with FLEXIBLE by the 3.2.6 and 3.3.4 parsers.
            ("object", True),
            ("OBJECT", True),
            ("option<object>", True),
            ("array<object>", True),
            ("set<object>", True),
            ("array<array<object>>", True),
            ("array<int> | object", True),
            ("array<option<object>>", True),
            ("{ a: int }", True),
            ("array<{ a: int }>", True),
            ("[int, object]", True),
            # Refused: "FLEXIBLE can only be used with types containing object".
            ("int", False),
            ("any", False),
            ("array<int>", False),
            ("record<user>", False),
            ("record<object>", False),
            ("option<record<object>>", False),
            ("record<object_store>", False),
            ("'object'", False),
        ],
    )
    def test_what_contains_an_object(self, field_type: str, contains: bool) -> None:
        """The parser's test: the ``object`` keyword or a literal object type, not a table name or a string."""
        assert operations._contains_object(field_type) is contains


class I195UnitTagged(BaseSurrealModel):
    """A list column: its element sub-field must follow ``define_table()`` too."""

    model_config = SurrealConfigDict(table_name="i195_unit_tagged")

    id: str | None = None
    tags: list[int]


class I195UnitAnyTagged(BaseSurrealModel):
    """``list[Any] | None`` maps to ``option<array<any>>``, which derives ``tags.*`` as ``any``."""

    model_config = SurrealConfigDict(table_name="i195_unit_any_tagged")

    id: str | None = None
    tags: list[Any] | None = None


class I195UnitPlainListTagged(BaseSurrealModel):
    """``list[Any]`` maps to ``array<any>``: the server derives no ``tags.*`` for it."""

    model_config = SurrealConfigDict(table_name="i195_unit_plain_list_tagged")

    id: str | None = None
    tags: list[Any] = []


class TestDefineTableAlignsElementSubFields:
    """The define_table() pair goes through the same ALTER renderer."""

    @pytest.mark.asyncio
    async def test_the_element_statement_follows_the_pair(self) -> None:
        client = _fake_client()
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager.get_client",
            new=AsyncMock(return_value=client),
        ):
            sql = await I195UnitTagged.define_table()

        assert sql.splitlines()[1:] == [
            "DEFINE FIELD IF NOT EXISTS tags ON i195_unit_tagged TYPE array<int>;",
            "ALTER FIELD tags ON i195_unit_tagged TYPE array<int> " + " ".join(ALL_DROPS[1:]) + ";",
            "ALTER FIELD IF EXISTS tags.* ON i195_unit_tagged TYPE int;",
        ]

    @pytest.mark.asyncio
    async def test_a_list_of_any_widens_the_element(self) -> None:
        """Replayed over ``list[int] | None``, ``tags.*`` would otherwise stay ``int``."""
        client = _fake_client()
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager.get_client",
            new=AsyncMock(return_value=client),
        ):
            sql = await I195UnitAnyTagged.define_table()

        assert sql.splitlines()[-1] == "ALTER FIELD IF EXISTS tags.* ON i195_unit_any_tagged TYPE any;"

    @pytest.mark.asyncio
    async def test_a_non_optional_list_of_any_leaves_the_element_alone(self) -> None:
        """No ``tags.*`` statement: one that exists was defined by hand and keeps its type."""
        client = _fake_client()
        with patch(
            "src.surreal_orm.model_base.SurrealDBConnectionManager.get_client",
            new=AsyncMock(return_value=client),
        ):
            sql = await I195UnitPlainListTagged.define_table()

        assert sql.splitlines()[-1].startswith("ALTER FIELD tags ON i195_unit_plain_list_tagged TYPE array<any> ")
        assert "tags.*" not in sql


class TestTheParserStopsAtPermissions:
    """A field PERMISSIONS clause is not modelled, and must not leak into the modelled ones.

    The clause splitter cut on any field keyword after whitespace, and
    SurrealDB reports a ``WHERE`` condition with field names unescaped
    (``type``, ``comment``, ``default``…). ``FOR select WHERE type = 'public'``
    therefore read as a TYPE clause of ``= 'public', …``: every schema_diff
    emitted an AlterField for the field, and its rollback did not parse.
    SurrealDB always reports PERMISSIONS last.
    """

    def test_a_where_naming_type_does_not_become_the_type(self) -> None:
        state = parse_define_field(
            "DEFINE FIELD secret ON pw TYPE string PERMISSIONS FOR select WHERE type = 'public', FOR create, update FULL"
        )

        assert state == FieldState(name="secret", field_type="string", nullable=False)

    def test_a_where_naming_other_clause_keywords_sets_none_of_them(self) -> None:
        state = parse_define_field(
            "DEFINE FIELD s ON t TYPE string PERMISSIONS FOR select WHERE comment = 'x' OR default = 1 "
            "OR readonly = true OR reference = 2 OR flexible = 3 OR assert = 4 OR `value` = 5 "
            "OR permissions = 6, FOR create FULL, FOR update NONE"
        )

        assert state == FieldState(name="s", field_type="string", nullable=False)

    def test_every_modelled_clause_survives_a_where_permission(self) -> None:
        """The clauses before PERMISSIONS parse exactly as without it."""
        head = (
            "DEFINE FIELD author ON posts TYPE none | record<users> DEFAULT users:root READONLY "
            "VALUE $value ASSERT $value != NONE REFERENCE ON DELETE CASCADE COMMENT 'who'"
        )

        restricted = parse_define_field(head + " PERMISSIONS FOR select WHERE type = 'public' OR comment = 'x'")

        assert restricted == parse_define_field(head + " PERMISSIONS FULL")
        assert restricted.comment == "who"
        assert restricted.on_delete == "CASCADE"

    def test_an_identifier_named_permissions_before_the_clause(self) -> None:
        """Only ``PERMISSIONS FULL|NONE|FOR`` starts the clause; a field called ``permissions`` does not."""
        state = parse_define_field(
            "DEFINE FIELD s ON t TYPE int VALUE permissions ASSERT permissions > 0 "
            "PERMISSIONS FOR select WHERE PERMISSIONS = 1, FOR create, update FULL"
        )

        assert state.value == "permissions"
        assert state.assertion == "permissions > 0"

    def test_the_keyword_inside_a_string_is_not_the_clause(self) -> None:
        state = parse_define_field(
            "DEFINE FIELD s ON t TYPE string DEFAULT 'a PERMISSIONS FULL b' COMMENT 'PERMISSIONS NONE here' "
            "PERMISSIONS FOR select, create, update NONE"
        )

        assert state.default == "a PERMISSIONS FULL b"
        assert state.comment == "PERMISSIONS NONE here"

    @pytest.mark.parametrize(
        ("default", "parsed"),
        [
            # SurrealDB escapes a quote inside a string with a backslash.
            (r"'a\'b PERMISSIONS FULL'", r"a\'b PERMISSIONS FULL"),
            ('"a\'b\\"c PERMISSIONS FULL"', "a'b\\\"c PERMISSIONS FULL"),
        ],
    )
    def test_an_escaped_quote_does_not_end_the_string(self, default: str, parsed: str) -> None:
        state = parse_define_field(f"DEFINE FIELD c ON t TYPE string DEFAULT {default} PERMISSIONS FOR select WHERE type = 'x'")

        assert state.field_type == "string"
        assert state.default == parsed

    def test_a_comparison_in_the_assertion_does_not_swallow_the_clause(self) -> None:
        """An unbalanced ``>`` kept the splitter from ever seeing PERMISSIONS, so it joined ASSERT."""
        state = parse_define_field(
            "DEFINE FIELD n ON t TYPE int ASSERT $value > 0 PERMISSIONS FOR select WHERE type = 'public', "
            "FOR create, update FULL"
        )

        assert state.assertion == "$value > 0"

    def test_a_statement_without_permissions_still_parses(self) -> None:
        """Hand-written statements need not carry the clause."""
        assert parse_define_field("DEFINE FIELD f ON t TYPE int ASSERT $value > 0").assertion == "$value > 0"
