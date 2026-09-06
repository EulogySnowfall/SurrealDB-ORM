"""
Unit tests for the table-definition round trip.

Reviewing what shipped in 0.33.1 turned up three defects that share one cause —
a ``DEFINE TABLE`` does not survive the trip to the server and back:

1. ``CreateTable.forwards()`` emitted the ``PERMISSIONS`` clause as a *second*
   ``DEFINE TABLE``, which SurrealDB rejects for an existing table
   (``The table 'x' already exists``). Declared table permissions therefore never
   reached the database at all — verified against 3.2.4 and 2.6.5, both of which
   stored ``PERMISSIONS NONE`` — and since the executor started surfacing
   statement errors, the first migration of any model using ``permissions={...}``
   aborts.
2. The "table definition changed" diff branch reuses ``CreateTable``, whose
   ``backwards()`` is ``REMOVE TABLE``. A migration that merely changed a
   permission had a rollback that dropped the table and every row in it — and
   reported ``reversible = True``.
3. ``_parse_permissions`` could not read back what the server reports
   (``FOR select WHERE $auth.id = id, FOR create, update, delete NONE``): the
   condition kept its trailing comma and the ``NONE`` group was dropped, so the
   diff never converged once permissions did reach the database.
"""

from src.surreal_orm.migrations.define_parser import parse_define_table
from src.surreal_orm.migrations.operations import CreateTable
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


class TestModifyingATableIsNotDestructive:
    """The modify path must not roll back into a DROP."""

    def test_overwrite_redefines_rather_than_recreates(self) -> None:
        """``DEFINE TABLE OVERWRITE`` updates an existing definition."""
        sql = CreateTable(name="users", overwrite=True, permissions={"select": "true"}).forwards()

        assert sql.startswith("DEFINE TABLE OVERWRITE users")

    def test_rollback_restores_the_previous_definition(self) -> None:
        """Not ``REMOVE TABLE`` — that dropped the table and all its rows."""
        op = CreateTable(
            name="users",
            overwrite=True,
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

    def test_the_diff_uses_the_non_destructive_form(self) -> None:
        """A permissions-only change must not produce a destructive rollback."""
        current = SchemaState()
        current.tables["Article"] = TableState(name="Article", permissions={"select": "$auth.id = id"})
        target = SchemaState()
        target.tables["Article"] = TableState(name="Article", permissions={"select": "true"})

        op = next(o for o in current.diff(target) if isinstance(o, CreateTable))

        assert op.forwards().startswith("DEFINE TABLE OVERWRITE Article")
        assert "REMOVE TABLE" not in op.backwards()
        assert "PERMISSIONS FOR select WHERE $auth.id = id" in op.backwards()


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

    def test_the_round_trip_converges(self) -> None:
        """Emit, parse back, and the states must compare equal."""
        model_permissions = {"select": "$auth.id = id"}
        emitted = CreateTable(name="s1", permissions=model_permissions).forwards()
        reparsed = parse_define_table(emitted.rstrip(";"))["permissions"]

        current = SchemaState()
        current.tables["s1"] = TableState(name="s1", permissions=reparsed)
        target = SchemaState()
        target.tables["s1"] = TableState(name="s1", permissions=model_permissions)

        assert [o for o in current.diff(target) if isinstance(o, CreateTable)] == []
