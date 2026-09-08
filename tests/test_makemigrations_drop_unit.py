"""
Unit tests for which operations ``makemigrations`` is willing to write.

Before 0.33.1 the current state was always empty, so ``diff()``'s "in self but
not in target" branches never fired from ``makemigrations``. Diffing against the
live database made them reachable for the first time, and they fire for every
database object with no registered model: the ORM's own migration history, graph
edge tables created by ``RELATE`` (``ModelIntrospector`` deliberately skips
``Relation`` fields, so an edge table can only ever look unmodelled), analyzers
and APIs, which ``ModelIntrospector`` never produces at all, and every table
whose model module simply was not imported. None of it is recoverable.

The partition lives in ``split_destructive`` so the CLI and the library agree,
and these tests exercise it directly — ``click`` is an optional extra, so
anything asserted only through the CLI runner is invisible to anyone without it.
"""

from src.surreal_orm.migrations.constants import MIGRATIONS_TABLE
from src.surreal_orm.migrations.operations import DropTable, split_destructive
from src.surreal_orm.migrations.state import AnalyzerState, SchemaState, TableState


def _db_with(*names: str, analyzers: tuple[str, ...] = ()) -> SchemaState:
    """Build the state a database read would produce."""
    state = SchemaState()
    for name in names:
        state.tables[name] = TableState(name=name)
    for analyzer in analyzers:
        state.analyzers[analyzer] = AnalyzerState(name=analyzer, tokenizers=["blank"])
    return state


class TestTheMigrationsTableIsNeverDropped:
    """It is ORM bookkeeping, never a user table — an invariant, not a policy."""

    def test_the_diff_never_proposes_dropping_it(self) -> None:
        """Applying that migration would erase the migration history."""
        operations = _db_with(MIGRATIONS_TABLE).diff(SchemaState())

        assert [op for op in operations if isinstance(op, DropTable)] == []

    def test_other_tables_are_still_drop_candidates(self) -> None:
        """``diff()`` stays a general API; only the caller decides policy."""
        operations = _db_with(MIGRATIONS_TABLE, "obsolete").diff(SchemaState())
        dropped = [op.name for op in operations if isinstance(op, DropTable)]

        assert dropped == ["obsolete"]


class TestSplitDestructive:
    """Irreversible operations are opt-in, whatever noun they act on."""

    def test_an_unmodelled_table_is_held_back(self) -> None:
        keep, destructive = split_destructive(_db_with("f1_edge").diff(SchemaState()))

        assert keep == []
        assert [op.describe() for op in destructive] == ["Drop table f1_edge"]

    def test_an_analyzer_is_held_back_too(self) -> None:
        """``ModelIntrospector`` never produces analyzers, so one always looks obsolete.

        Enumerating ``DropTable`` alone let this one through: ``makemigrations``
        wrote ``REMOVE ANALYZER`` into the file on every run, with no "Skipped"
        line, and ``migrate`` then removed an analyzer that full-text indexes
        reference.
        """
        operations = _db_with(analyzers=("english",)).diff(SchemaState())
        keep, destructive = split_destructive(operations)

        assert keep == []
        assert [op.describe() for op in destructive] == ["Remove analyzer english"]

    def test_reversible_operations_are_kept(self) -> None:
        """Only what cannot be rolled back needs the opt-in."""
        current = SchemaState()
        current.tables["t"] = TableState(name="t", schema_mode="SCHEMALESS")
        target = SchemaState()
        target.tables["t"] = TableState(name="t", schema_mode="SCHEMAFULL")

        keep, destructive = split_destructive(current.diff(target))

        assert destructive == []
        assert keep != []

    def test_the_partition_is_total(self) -> None:
        """Nothing is dropped on the floor by the split itself."""
        operations = _db_with("obsolete", analyzers=("english",)).diff(SchemaState())
        keep, destructive = split_destructive(operations)

        assert len(keep) + len(destructive) == len(operations)
        assert all(op.reversible for op in keep)
        assert not any(op.reversible for op in destructive)
