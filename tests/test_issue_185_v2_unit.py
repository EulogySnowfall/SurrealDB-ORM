"""
Unit tests for the #185 backport onto the v2 (SurrealDB 2.6.x) line.

Both defects were reproduced against a live SurrealDB 2.6.5 before porting.

``makemigrations`` built its current state as a bare ``SchemaState()``, so it
re-emitted every table on every run. And even once it read the database, a second
route produced the same symptom: 2.6.x reports ``PERMISSIONS NONE`` for a table
defined without a ``PERMISSIONS`` clause, which the parser expands to
``{"select": "NONE", "create": "NONE", "update": "NONE", "delete": "NONE"}`` —
a state that compares unequal to a model configuring nothing::

    db.permissions    = {'select': 'NONE', 'create': 'NONE', 'update': 'NONE', 'delete': 'NONE'}
    model.permissions = {}
    -> ['CreateTable']

This lands after the #179 backport on purpose: diffing against the database while
the model side still lost optionality would have proposed a phantom ``AlterField``
on every run.
"""

from src.surreal_orm.migrations.operations import CreateTable
from src.surreal_orm.migrations.state import SchemaState, TableState

#: What SurrealDB 2.6.x reports for a table defined with no PERMISSIONS clause.
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
