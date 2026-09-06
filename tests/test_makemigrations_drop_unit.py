"""
Unit tests for which tables ``makemigrations`` is willing to drop.

Before 0.33.1 the current state was always empty, so ``diff()``'s "tables in
self but not in target" branch never fired from ``makemigrations``. Diffing
against the live database made it reachable for the first time, and it fires for
**every** table with no registered model:

    DropTable proposés  : ['_surreal_orm_migrations', 'f1_edge']
    forwards() : REMOVE TABLE _surreal_orm_migrations;
    backwards(): ''            reversible = False

That includes the ORM's own migration history, graph edge tables created by
``RELATE`` (``ModelIntrospector`` deliberately skips ``Relation`` fields, so an
edge table can only ever look unmodelled), and every table whose model module
simply was not imported. None of it is recoverable.
"""

from src.surreal_orm.migrations.executor import MIGRATIONS_TABLE
from src.surreal_orm.migrations.operations import DropTable
from src.surreal_orm.migrations.state import SchemaState, TableState


def _db_with(*names: str) -> SchemaState:
    state = SchemaState()
    for name in names:
        state.tables[name] = TableState(name=name)
    return state


class TestTheMigrationsTableIsNeverDropped:
    """It is ORM bookkeeping, never a user table — an invariant, not a policy."""

    def test_the_diff_never_proposes_dropping_it(self) -> None:
        """Applying that migration would erase the migration history."""
        operations = _db_with(MIGRATIONS_TABLE).diff(SchemaState())

        assert [op for op in operations if isinstance(op, DropTable)] == []

    def test_other_tables_are_still_drop_candidates(self) -> None:
        """``diff()`` stays a general API; only the CLI decides policy."""
        operations = _db_with(MIGRATIONS_TABLE, "obsolete").diff(SchemaState())
        dropped = [op.name for op in operations if isinstance(op, DropTable)]

        assert dropped == ["obsolete"]
