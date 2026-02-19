"""
Migration executor for applying migrations to the database.

This module handles:
- Tracking applied migrations in the database
- Applying pending migrations
- Rolling back migrations
- Executing data migrations (upgrade command)
"""

import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..connection_manager import SurrealDBConnectionManager
from .migration import Migration, parse_migration_name
from .operations import DataMigration

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Table name for tracking migrations
MIGRATIONS_TABLE = "_surreal_orm_migrations"


class MigrationExecutor:
    """
    Executes migrations against the database.

    Handles applying, rolling back, and tracking migrations.
    """

    def __init__(self, migrations_dir: Path | str):
        """
        Initialize the executor.

        Args:
            migrations_dir: Path to the migrations directory
        """
        self.migrations_dir = Path(migrations_dir)

    async def ensure_migrations_table(self) -> None:
        """Create the migrations tracking table if it doesn't exist."""
        client = await SurrealDBConnectionManager.get_client()

        await client.query(f"""
            DEFINE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} SCHEMAFULL;
            DEFINE FIELD name ON {MIGRATIONS_TABLE} TYPE string;
            DEFINE FIELD applied_at ON {MIGRATIONS_TABLE} TYPE datetime DEFAULT time::now();
            DEFINE INDEX migration_name ON {MIGRATIONS_TABLE} FIELDS name UNIQUE;
        """)

    async def get_applied_migrations(self) -> list[str]:
        """
        Get list of already applied migration names.

        Returns:
            List of migration names that have been applied
        """
        await self.ensure_migrations_table()

        client = await SurrealDBConnectionManager.get_client()
        result = await client.query(f"SELECT name, applied_at FROM {MIGRATIONS_TABLE} ORDER BY applied_at;")

        if result.is_empty:
            return []

        return [r["name"] for r in result.all_records]

    def get_available_migrations(self) -> list[str]:
        """
        Get list of all migration files in the migrations directory.

        Returns:
            List of migration names sorted by number
        """
        if not self.migrations_dir.exists():
            return []

        migrations = []
        for filepath in self.migrations_dir.glob("*.py"):
            name = filepath.stem
            if name.startswith("_"):
                continue
            try:
                parse_migration_name(name)
                migrations.append(name)
            except ValueError:
                continue

        return sorted(migrations)

    def get_pending_migrations(self, applied: list[str]) -> list[str]:
        """
        Get list of migrations that haven't been applied yet.

        Args:
            applied: List of already applied migration names

        Returns:
            List of pending migration names
        """
        available = self.get_available_migrations()
        return [m for m in available if m not in applied]

    def load_migration(self, name: str) -> Migration:
        """
        Load a migration from file.

        Args:
            name: Migration name (e.g., "0001_initial")

        Returns:
            Migration object

        Raises:
            FileNotFoundError: If migration file doesn't exist
            ImportError: If migration file is invalid
        """
        filepath = self.migrations_dir / f"{name}.py"

        if not filepath.exists():
            raise FileNotFoundError(f"Migration file not found: {filepath}")

        spec = importlib.util.spec_from_file_location(name, filepath)
        if not spec or not spec.loader:
            raise ImportError(f"Could not load migration: {name}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        migration = getattr(module, "migration", None)
        # Check by class name to handle different import paths (src.surreal_orm vs surreal_orm)
        if migration is None or type(migration).__name__ != "Migration":
            raise ImportError(f"Migration file must define 'migration' variable: {name}")

        return migration  # type: ignore[no-any-return]

    def _sort_by_dependencies(self, migrations: list[Migration]) -> list[Migration]:
        """
        Topologically sort migrations by dependencies.

        Args:
            migrations: List of migrations to sort

        Returns:
            Sorted list of migrations
        """
        sorted_migrations: list[Migration] = []
        remaining = migrations.copy()
        seen: set[str] = set()
        max_iterations = len(migrations) * 2

        iteration = 0
        while remaining and iteration < max_iterations:
            iteration += 1
            for m in remaining:
                if all(dep in seen for dep in m.dependencies):
                    sorted_migrations.append(m)
                    seen.add(m.name)
                    remaining.remove(m)
                    break

        if remaining:
            raise ValueError(f"Circular dependency detected in migrations: {[m.name for m in remaining]}")

        return sorted_migrations

    async def migrate(
        self,
        target: str | None = None,
        fake: bool = False,
        schema_only: bool = True,
    ) -> list[str]:
        """
        Apply pending migrations.

        Args:
            target: Optional target migration name. If None, apply all pending.
            fake: If True, mark as applied without executing.
            schema_only: If True, only apply schema operations (DDL).
                        If False, also apply data migrations (DML).

        Returns:
            List of applied migration names
        """
        await self.ensure_migrations_table()

        applied = await self.get_applied_migrations()
        pending_names = self.get_pending_migrations(applied)

        if target:
            # Filter to migrations up to and including target
            pending_names = [n for n in pending_names if n <= target]

        if not pending_names:
            logger.info("No migrations to apply.")
            return []

        # Load and sort migrations
        pending_migrations = [self.load_migration(name) for name in pending_names]
        sorted_migrations = self._sort_by_dependencies(pending_migrations)

        client = await SurrealDBConnectionManager.get_client()
        applied_names: list[str] = []

        for migration in sorted_migrations:
            logger.info(f"Applying migration: {migration.name}")

            if not fake:
                # Get operations to execute
                if schema_only:
                    operations = migration.schema_operations
                else:
                    operations = migration.operations

                # Execute each operation
                for op in operations:
                    sql = op.forwards()
                    if sql:
                        logger.debug(f"Executing: {sql[:100]}...")
                        try:
                            await client.query(sql)
                        except Exception as e:
                            logger.error(f"Migration failed at {op.describe()}: {e}")
                            raise

                    # Handle async data migrations
                    if isinstance(op, DataMigration) and op.forwards_func:
                        await op.forwards_func()

            # Record migration as applied
            await client.query(
                f"CREATE {MIGRATIONS_TABLE} SET name = $name;",
                {"name": migration.name},
            )

            applied_names.append(migration.name)
            logger.info(f"Applied: {migration.name}")

        return applied_names

    async def rollback(self, target: str) -> list[str]:
        """
        Rollback migrations to target.

        Args:
            target: Target migration name to rollback to.
                    Migrations after this will be rolled back.

        Returns:
            List of rolled back migration names
        """
        await self.ensure_migrations_table()

        applied = await self.get_applied_migrations()

        # Get migrations to rollback (in reverse order)
        to_rollback = [name for name in reversed(applied) if name > target]

        if not to_rollback:
            logger.info("No migrations to rollback.")
            return []

        client = await SurrealDBConnectionManager.get_client()
        rolled_back: list[str] = []

        for name in to_rollback:
            migration = self.load_migration(name)

            if not migration.is_reversible:
                raise ValueError(f"Migration {name} is not reversible")

            logger.info(f"Rolling back: {name}")

            # Execute backwards SQL
            for sql in migration.backwards_sql():
                if sql:
                    logger.debug(f"Executing: {sql[:100]}...")
                    try:
                        await client.query(sql)
                    except Exception as e:
                        logger.error(f"Rollback failed for migration {name}, SQL: {sql[:200]}... Error: {e}")
                        raise RuntimeError(f"Rollback failed for migration {name}: {e}") from e

            # Handle async data migrations backwards
            for op in reversed(migration.operations):
                if isinstance(op, DataMigration) and op.backwards_func:
                    await op.backwards_func()

            # Remove migration record
            await client.query(
                f"DELETE {MIGRATIONS_TABLE} WHERE name = $name;",
                {"name": name},
            )

            rolled_back.append(name)
            logger.info(f"Rolled back: {name}")

        return rolled_back

    async def upgrade(self, target: str | None = None) -> list[str]:
        """
        Apply data migrations only.

        This is separate from schema migrations to allow running
        data transformations independently.

        Args:
            target: Optional target migration name

        Returns:
            List of migrations whose data operations were applied
        """
        await self.ensure_migrations_table()

        applied = await self.get_applied_migrations()
        applied_names: list[str] = []

        client = await SurrealDBConnectionManager.get_client()

        for name in applied:
            if target and name > target:
                break

            migration = self.load_migration(name)

            if not migration.has_data_migrations:
                continue

            logger.info(f"Running data migrations for: {name}")

            for op in migration.data_operations:
                sql = op.forwards()
                if sql:
                    logger.debug(f"Executing: {sql[:100]}...")
                    await client.query(sql)

                if isinstance(op, DataMigration) and op.forwards_func:
                    await op.forwards_func()

            applied_names.append(name)

        return applied_names

    async def get_migration_status(self) -> dict[str, dict[str, bool | int]]:
        """
        Get status of all migrations.

        Returns:
            Dict mapping migration name to status info
        """
        applied = await self.get_applied_migrations()
        available = self.get_available_migrations()

        status = {}
        for name in available:
            is_applied = name in applied
            migration = self.load_migration(name)
            status[name] = {
                "applied": is_applied,
                "reversible": migration.is_reversible,
                "has_data": migration.has_data_migrations,
                "operations": len(migration.operations),
            }

        return status

    async def show_sql(self, name: str) -> str:
        """
        Show the SQL that would be executed for a migration.

        Args:
            name: Migration name

        Returns:
            SQL statements as a string
        """
        migration = self.load_migration(name)
        statements = migration.forwards_sql()
        return "\n\n".join(statements)
