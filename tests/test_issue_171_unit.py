"""
Unit tests for issue #171 — ``makemigrations`` diffed against an empty schema.

The CLI half lives in ``tests/test_cli.py``. This file covers the comparison the
fix depends on: SurrealDB reports its *default* permissions block for a table
defined without a ``PERMISSIONS`` clause, so a state read back from the database
compared unequal to the model state and ``diff()`` re-emitted ``CreateTable`` for
every table — the same symptom #171 reports, by a second route.
"""

from src.surreal_orm.migrations.operations import CreateTable
from src.surreal_orm.migrations.state import SchemaState, TableState

#: What SurrealDB reports for a table defined with no PERMISSIONS clause.
SURREAL_DEFAULT_PERMISSIONS = {"select": "NONE", "create": "NONE", "update": "NONE", "delete": "NONE"}


def _states(current_permissions, target_permissions):
    current = SchemaState()
    current.tables["articles"] = TableState(name="articles", permissions=current_permissions)
    target = SchemaState()
    target.tables["articles"] = TableState(name="articles", permissions=target_permissions)
    return current, target


class TestDefaultPermissionsAreNotAChange:
    """SurrealDB's implicit NONE block must not read as a configured value."""

    def test_default_permissions_do_not_diff_against_none_configured(self) -> None:
        """The database's default block equals a model that configures nothing."""
        current, target = _states(SURREAL_DEFAULT_PERMISSIONS, {})

        assert [op for op in current.diff(target) if isinstance(op, CreateTable)] == []

    def test_explicitly_setting_none_is_also_the_default(self) -> None:
        """Spelling out NONE is the same schema, so it is not a change either."""
        current, target = _states(SURREAL_DEFAULT_PERMISSIONS, {"select": "NONE"})

        assert [op for op in current.diff(target) if isinstance(op, CreateTable)] == []

    def test_a_real_permission_still_diffs(self) -> None:
        """A configured rule must still be detected against the default block."""
        current, target = _states(SURREAL_DEFAULT_PERMISSIONS, {"select": "$auth.id = id"})

        assert [op for op in current.diff(target) if isinstance(op, CreateTable)]

    def test_removing_a_permission_still_diffs(self) -> None:
        """Dropping a configured rule is a change in the other direction."""
        current, target = _states({"select": "$auth.id = id"}, {})

        assert [op for op in current.diff(target) if isinstance(op, CreateTable)]
