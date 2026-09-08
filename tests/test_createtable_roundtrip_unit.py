"""
Unit tests for the table-definition round trip.

A ``DEFINE TABLE`` has to survive the trip to the server and back: what the ORM
emits, what SurrealDB stores, what ``INFO FOR DB`` reports and what the parser
reads must all agree, or the diff either never converges or converges on the
wrong definition.

Two families of defect are pinned here. The first is the clause reaching the
database at all: ``PERMISSIONS`` used to be emitted as a *second*
``DEFINE TABLE``, which the server rejects once the table exists. The second is
subtler and worse — a clause that reaches the database meaning something other
than what the model said. ``FOR select WHERE FULL`` is accepted, stored, and
denies every read, because ``FULL`` in an expression is a field reference that
evaluates to ``NONE``.
"""

import pytest

from src.surreal_orm.migrations.define_parser import parse_define_table
from src.surreal_orm.migrations.operations import AlterTable, CreateTable
from src.surreal_orm.migrations.state import SchemaState, TableState

#: What SurrealDB reports for a table defined with one explicit rule.
SERVER_REPORTED = (
    "DEFINE TABLE s1 TYPE NORMAL SCHEMAFULL PERMISSIONS FOR select WHERE $auth.id = id, FOR create, update, delete NONE"
)


class TestPermissionsReachTheDatabase:
    """The clause belongs to the DEFINE TABLE, not to a second statement."""

    def test_permissions_are_a_clause_of_the_single_statement(self) -> None:
        """A second ``DEFINE TABLE`` is rejected once the table exists."""
        sql = CreateTable(name="users", permissions={"select": "$auth.id = id"}).forwards()

        assert sql.count("DEFINE TABLE") == 1, sql
        assert "PERMISSIONS FOR select WHERE $auth.id = id" in sql

    def test_a_table_without_permissions_is_unchanged(self) -> None:
        """No clause when nothing is configured."""
        sql = CreateTable(name="users").forwards()

        assert sql == "DEFINE TABLE users SCHEMAFULL;"

    def test_permissions_come_after_the_schema_mode(self) -> None:
        """Clause order the server accepts."""
        sql = CreateTable(name="users", changefeed="7d", permissions={"select": "true"}).forwards()

        assert sql.index("SCHEMAFULL") < sql.index("PERMISSIONS")
        assert "CHANGEFEED 7d" in sql


class TestKeywordPermissionsAreNotConditions:
    """``FULL`` and ``NONE`` are keywords; rendering them as a WHERE inverts them."""

    @pytest.mark.parametrize("keyword", ["FULL", "NONE"])
    def test_a_keyword_rule_has_no_where(self, keyword: str) -> None:
        """``FOR select WHERE FULL`` parses, stores, and then denies every read."""
        sql = CreateTable(name="pw", permissions={"select": keyword}).forwards()

        assert f"FOR select {keyword}" in sql
        assert "WHERE" not in sql, sql

    def test_a_condition_still_gets_its_where(self) -> None:
        """Only the two keywords are special."""
        sql = CreateTable(name="pw", permissions={"select": "$auth.id = id"}).forwards()

        assert "FOR select WHERE $auth.id = id" in sql

    def test_a_keyword_survives_the_round_trip(self) -> None:
        """Emit, parse back, and the rule must still be the keyword."""
        emitted = CreateTable(name="pw", permissions={"select": "FULL"}).forwards()

        assert parse_define_table(emitted.rstrip(";"))["permissions"]["select"] == "FULL"


class TestOrmOnlyTableTypesAreNotEmitted:
    """USER, STREAM and HASH classify a model; SurrealDB does not know them."""

    @pytest.mark.parametrize("table_type", ["user", "stream", "hash", "normal"])
    def test_the_type_clause_is_dropped(self, table_type: str) -> None:
        """``TYPE USER`` is a parse error, not a definition."""
        assert "TYPE" not in CreateTable(name="acct", table_type=table_type).forwards()

    @pytest.mark.parametrize("table_type", ["relation", "any"])
    def test_a_real_surreal_type_is_kept(self, table_type: str) -> None:
        assert f"TYPE {table_type.upper()}" in CreateTable(name="t", table_type=table_type).forwards()

    def test_the_diff_does_not_forward_an_orm_type(self) -> None:
        """Any change on a USER table was unmigratable."""
        current = TableState(name="acct", schema_mode="SCHEMALESS", table_type="user")
        target = TableState(name="acct", schema_mode="SCHEMAFULL", table_type="user")

        sql = AlterTable.from_table_states(current, target).forwards()

        assert "TYPE USER" not in sql
        assert sql == "DEFINE TABLE OVERWRITE acct SCHEMAFULL;"


class TestModifyingATableIsNotDestructive:
    """The modify path must not roll back into a DROP."""

    def test_overwrite_redefines_rather_than_recreates(self) -> None:
        """``DEFINE TABLE OVERWRITE`` updates an existing definition."""
        sql = AlterTable(name="users", schema_mode="SCHEMAFULL", permissions={"select": "true"}).forwards()

        assert sql.startswith("DEFINE TABLE OVERWRITE users")

    def test_rollback_restores_the_previous_definition(self) -> None:
        """Not ``REMOVE TABLE`` — that dropped the table and all its rows."""
        op = AlterTable(
            name="users",
            schema_mode="SCHEMAFULL",
            permissions={"select": "true"},
            previous_schema_mode="SCHEMAFULL",
            previous_permissions={"select": "$auth.id = id"},
        )

        rollback = op.backwards()

        assert "REMOVE TABLE" not in rollback, rollback
        assert rollback.startswith("DEFINE TABLE OVERWRITE users")
        assert "PERMISSIONS FOR select WHERE $auth.id = id" in rollback

    def test_creating_a_new_table_still_rolls_back_to_a_drop(self) -> None:
        """A table this migration created should still be removed on rollback."""
        assert CreateTable(name="users").backwards() == "REMOVE TABLE users;"

    def test_an_alter_without_a_previous_definition_is_irreversible(self) -> None:
        """A bare ``DEFINE TABLE OVERWRITE t;`` resets the table, it does not restore it."""
        op = AlterTable(name="c", permissions={"select": "FULL"})

        assert op.reversible is False
        with pytest.raises(ValueError, match="no previous definition"):
            op.backwards()

    def test_the_diff_uses_the_non_destructive_form(self) -> None:
        """A permissions-only change must not produce a destructive rollback."""
        current = SchemaState()
        current.tables["Article"] = TableState(name="Article", permissions={"select": "$auth.id = id"})
        target = SchemaState()
        target.tables["Article"] = TableState(name="Article", permissions={"select": "true"})

        op = next(o for o in current.diff(target) if isinstance(o, AlterTable))

        assert op.reversible is True
        assert op.forwards().startswith("DEFINE TABLE OVERWRITE Article")
        assert "REMOVE TABLE" not in op.backwards()
        assert "PERMISSIONS FOR select WHERE $auth.id = id" in op.backwards()

    def test_a_redefinition_is_described_as_one(self) -> None:
        """``makemigrations`` and the executor label the operation for the reader."""
        current = TableState(name="Article", schema_mode="SCHEMALESS")
        target = TableState(name="Article", schema_mode="SCHEMAFULL")

        assert AlterTable.from_table_states(current, target).describe() == "Alter table Article"
        assert CreateTable(name="Article").describe() == "Create table Article"


class TestTheDefinitionTravelsWhole:
    """Every clause must reach both directions of the operation."""

    def test_a_view_is_not_flattened_into_a_plain_table(self) -> None:
        """An overwrite with no AS clause turns the view into a normal table."""
        current = TableState(name="v", schema_mode="SCHEMALESS", view_query="SELECT n FROM src")
        target = TableState(name="v", schema_mode="SCHEMAFULL", view_query="SELECT n FROM src")

        op = AlterTable.from_table_states(current, target)

        assert "AS (SELECT n FROM src)" in op.forwards()
        assert "AS (SELECT n FROM src)" in op.backwards()

    def test_a_comment_survives_both_directions(self) -> None:
        """COMMENT had no previous slot, so an overwrite dropped it either way."""
        current = TableState(name="t", comment="before")
        target = TableState(name="t", comment="after")

        op = AlterTable.from_table_states(current, target)

        assert "COMMENT 'after'" in op.forwards()
        assert "COMMENT 'before'" in op.backwards()

    def test_a_relation_list_is_pipe_separated(self) -> None:
        """``relation_out=["a", "b"]`` is the documented config form."""
        sql = CreateTable(
            name="likes",
            table_type="relation",
            relation_in="person",
            relation_out=["blog_post", "book"],
        ).forwards()

        assert "OUT blog_post | book" in sql

    def test_the_previous_permissions_drop_the_server_defaults(self) -> None:
        """The server reports three default NONE entries; they are not configuration."""
        current = TableState(
            name="t",
            permissions={"select": "$auth.id = id", "create": "NONE", "update": "NONE", "delete": "NONE"},
        )
        target = TableState(name="t", permissions={"select": "true"})

        rollback = AlterTable.from_table_states(current, target).backwards()

        assert "FOR create NONE" not in rollback, rollback
        assert "PERMISSIONS FOR select WHERE $auth.id = id" in rollback


class TestPermissionsParseBackFromTheServer:
    """``_parse_permissions`` must read the shape ``INFO FOR DB`` returns."""

    def test_a_rule_keeps_no_trailing_comma(self) -> None:
        """The comma separating the groups leaked into the condition."""
        parsed = parse_define_table(SERVER_REPORTED)["permissions"]

        assert parsed["select"] == "$auth.id = id"

    def test_the_grouped_none_actions_are_parsed(self) -> None:
        """``FOR create, update, delete NONE`` has no WHERE and was dropped."""
        parsed = parse_define_table(SERVER_REPORTED)["permissions"]

        assert parsed["create"] == "NONE"
        assert parsed["update"] == "NONE"
        assert parsed["delete"] == "NONE"

    def test_a_grouped_full_is_parsed(self) -> None:
        """``FULL`` is the other keyword form the server can report."""
        parsed = parse_define_table("DEFINE TABLE t SCHEMAFULL PERMISSIONS FOR select, create FULL")["permissions"]

        assert parsed == {"select": "FULL", "create": "FULL"}

    def test_a_quoted_for_does_not_start_a_group(self) -> None:
        """Splitting on the bare token truncated the condition at the quote."""
        parsed = parse_define_table(
            "DEFINE TABLE t SCHEMAFULL PERMISSIONS FOR select WHERE label = 'FOR sale', FOR create NONE"
        )["permissions"]

        assert parsed["select"] == "label = 'FOR sale'"
        assert parsed["create"] == "NONE"

    def test_a_keyword_inside_a_condition_does_not_end_the_clause(self) -> None:
        """``meta.type`` cut the clause, leaving ``meta.`` and a bogus table type."""
        parsed = parse_define_table("DEFINE TABLE t SCHEMAFULL PERMISSIONS FOR select WHERE meta.type != NONE")

        assert parsed["permissions"]["select"] == "meta.type != NONE"
        assert parsed["table_type"] == "normal"

    def test_the_round_trip_converges(self) -> None:
        """Emit, parse back, and the states must compare equal."""
        model_permissions = {"select": "$auth.id = id"}
        emitted = CreateTable(name="s1", permissions=model_permissions).forwards()
        reparsed = parse_define_table(emitted.rstrip(";"))["permissions"]

        current = SchemaState()
        current.tables["s1"] = TableState(name="s1", permissions=reparsed)
        target = SchemaState()
        target.tables["s1"] = TableState(name="s1", permissions=model_permissions)

        assert [o for o in current.diff(target) if isinstance(o, AlterTable)] == []
