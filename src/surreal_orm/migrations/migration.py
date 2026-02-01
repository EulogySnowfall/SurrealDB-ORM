"""
Migration class and utilities for SurrealDB schema migrations.

A Migration represents a set of operations to be applied to the database
in a specific order, with optional dependencies on other migrations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .operations import Operation


@dataclass
class Migration:
    """
    Represents a single migration containing a list of operations.

    Migrations are stored as Python files and can be applied forwards
    (to update the schema) or backwards (to rollback changes).

    Attributes:
        name: Unique identifier for the migration (e.g., "0001_initial")
        dependencies: List of migration names this depends on
        operations: List of operations to apply
        created_at: Timestamp when the migration was created

    Example:
        migration = Migration(
            name="0001_initial",
            dependencies=[],
            operations=[
                CreateTable(name="User", schema_mode="SCHEMAFULL"),
                AddField(table="User", name="email", field_type="string"),
            ],
        )
    """

    name: str
    dependencies: list[str] = field(default_factory=list)
    operations: list["Operation"] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def forwards_sql(self) -> list[str]:
        """
        Generate all forward migration SQL statements.

        Returns:
            List of SurrealQL statements to apply the migration
        """
        statements = []
        for op in self.operations:
            sql = op.forwards()
            if sql:
                statements.append(sql)
        return statements

    def backwards_sql(self) -> list[str]:
        """
        Generate all rollback SQL statements.

        Operations are reversed in order for proper rollback.

        Returns:
            List of SurrealQL statements to rollback the migration
        """
        statements = []
        for op in reversed(self.operations):
            if op.reversible:
                sql = op.backwards()
                if sql:
                    statements.append(sql)
        return statements

    @property
    def is_reversible(self) -> bool:
        """
        Check if all operations in this migration are reversible.

        Returns:
            True if the entire migration can be rolled back
        """
        return all(op.reversible for op in self.operations)

    @property
    def has_data_migrations(self) -> bool:
        """
        Check if this migration contains data migrations.

        Data migrations modify existing records rather than schema.

        Returns:
            True if the migration contains DataMigration operations
        """
        from .operations import DataMigration

        return any(isinstance(op, DataMigration) for op in self.operations)

    @property
    def schema_operations(self) -> list["Operation"]:
        """
        Get only schema-modifying operations (DDL).

        Returns:
            List of operations that modify schema (not data)
        """
        from .operations import DataMigration

        return [op for op in self.operations if not isinstance(op, DataMigration)]

    @property
    def data_operations(self) -> list["Operation"]:
        """
        Get only data-modifying operations (DML).

        Returns:
            List of DataMigration operations
        """
        from .operations import DataMigration

        return [op for op in self.operations if isinstance(op, DataMigration)]

    def describe(self) -> str:
        """
        Get a human-readable description of this migration.

        Returns:
            Multi-line string describing all operations
        """
        lines = [f"Migration: {self.name}"]
        if self.dependencies:
            lines.append(f"Dependencies: {', '.join(self.dependencies)}")
        lines.append(f"Operations ({len(self.operations)}):")
        for i, op in enumerate(self.operations, 1):
            lines.append(f"  {i}. {op.describe()}")
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"Migration(name={self.name!r}, operations={len(self.operations)})"


def parse_migration_name(filename: str) -> tuple[int, str]:
    """
    Parse a migration filename into number and name.

    Args:
        filename: Migration filename (e.g., "0001_initial.py")

    Returns:
        Tuple of (migration_number, migration_name)

    Raises:
        ValueError: If filename doesn't match expected format
    """
    # Remove .py extension if present
    if filename.endswith(".py"):
        filename = filename[:-3]

    parts = filename.split("_", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid migration filename: {filename}")

    try:
        number = int(parts[0])
    except ValueError:
        raise ValueError(f"Invalid migration number in: {filename}")

    return number, parts[1]


def generate_migration_name(number: int, name: str) -> str:
    """
    Generate a migration filename from number and name.

    Args:
        number: Migration sequence number
        name: Descriptive name for the migration

    Returns:
        Formatted filename (e.g., "0001_initial")
    """
    # Sanitize name: lowercase, replace spaces with underscores
    safe_name = name.lower().replace(" ", "_").replace("-", "_")
    # Remove any non-alphanumeric characters except underscores
    safe_name = "".join(c for c in safe_name if c.isalnum() or c == "_")
    return f"{number:04d}_{safe_name}"
