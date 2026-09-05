"""
Unit tests for the #168 backport onto the v2 (SurrealDB 2.6.x) line.

Two defects, both reproduced against a real SurrealDB 2.6.5 before porting:

1. ``AlterField`` emitted a plain ``DEFINE FIELD``, which does not update an
   existing field — the server answers ``The field 'a' already exists`` and
   leaves the definition untouched, so every alter was a silent no-op.
2. ``client.query()`` only raises on an RPC-level failure. A rejected statement
   rides back inside a *successful* RPC as ``status: ERR`` per statement, so the
   executor reported success for migrations that changed nothing.
"""

import pytest

from src.surreal_orm.migrations.executor import MigrationStatementError, _check_statements
from src.surreal_orm.migrations.operations import AddField, AlterField
from src.surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus


class TestAlterFieldOverwrites:
    """``OVERWRITE`` is required, not optional — plain DEFINE FIELD is a no-op."""

    def test_forwards_overwrites(self) -> None:
        """An alter must actually redefine the field."""
        op = AlterField(table="users", name="age", field_type="int")

        assert op.forwards().startswith("DEFINE FIELD OVERWRITE age ON users")

    def test_backwards_overwrites(self) -> None:
        """A rollback re-defining the previous type is otherwise a no-op too."""
        op = AlterField(table="users", name="age", field_type="int", previous_type="string")

        assert op.backwards().startswith("DEFINE FIELD OVERWRITE age ON users")

    def test_add_field_does_not_overwrite(self) -> None:
        """``AddField`` deliberately keeps plain DEFINE FIELD.

        Adding a field that already exists should be an error the operator sees,
        not a silent redefinition of someone else's column.
        """
        op = AddField(table="users", name="email", field_type="string")

        assert "OVERWRITE" not in op.forwards()


class TestStatementErrorsSurface:
    """A rejected statement inside a successful RPC must not pass for success."""

    def test_a_rejected_statement_raises(self) -> None:
        """This is the shape SurrealDB returns for a duplicate DEFINE FIELD."""
        response = QueryResponse(
            results=[
                QueryResult(status=ResponseStatus.OK, result=None),
                QueryResult(status=ResponseStatus.ERR, result="The field 'a' already exists"),
            ]
        )

        with pytest.raises(MigrationStatementError, match="already exists"):
            _check_statements(response, "DEFINE FIELD a ON r TYPE int;", "migration 0001")

    def test_the_context_and_sql_reach_the_message(self) -> None:
        """An operator needs to know which migration and which statement."""
        response = QueryResponse(results=[QueryResult(status=ResponseStatus.ERR, result="boom")])

        with pytest.raises(MigrationStatementError) as excinfo:
            _check_statements(response, "DEFINE FIELD a ON r TYPE int;", "migration 0007")

        assert "migration 0007" in str(excinfo.value)
        assert "DEFINE FIELD a ON r TYPE int;" in str(excinfo.value)

    def test_an_all_ok_response_passes(self) -> None:
        """The happy path stays silent."""
        response = QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=None)])

        _check_statements(response, "DEFINE TABLE t;", "migration 0001")

    def test_a_response_without_results_passes(self) -> None:
        """Some calls return no per-statement results; that is not an error."""
        _check_statements(QueryResponse(), "DEFINE TABLE t;", "migration 0001")
