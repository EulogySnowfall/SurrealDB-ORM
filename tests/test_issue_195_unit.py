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
