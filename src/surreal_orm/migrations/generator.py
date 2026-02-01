"""
Migration file generator.

This module generates Python migration files from a list of operations.
The generated files follow Django's migration format and can be edited
before being applied.
"""

from dataclasses import MISSING
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .migration import generate_migration_name

if TYPE_CHECKING:
    from .operations import Operation


class MigrationGenerator:
    """
    Generates Python migration files from operations.

    The generated files can be reviewed and edited before being applied
    to the database.
    """

    def __init__(self, migrations_dir: Path | str):
        """
        Initialize the generator with a migrations directory.

        Args:
            migrations_dir: Path to the migrations directory
        """
        self.migrations_dir = Path(migrations_dir)

    def ensure_directory(self) -> None:
        """Create the migrations directory if it doesn't exist."""
        self.migrations_dir.mkdir(parents=True, exist_ok=True)

        # Create __init__.py if it doesn't exist
        init_file = self.migrations_dir / "__init__.py"
        if not init_file.exists():
            init_file.write_text('"""Auto-generated migrations package."""\n')

    def get_next_number(self) -> int:
        """
        Get the next migration number.

        Returns:
            Next available migration number
        """
        self.ensure_directory()

        existing = list(self.migrations_dir.glob("*.py"))
        max_num = 0

        for filepath in existing:
            name = filepath.stem
            if name.startswith("_"):
                continue
            try:
                parts = name.split("_", 1)
                if parts[0].isdigit():
                    num = int(parts[0])
                    max_num = max(max_num, num)
            except (ValueError, IndexError):
                continue

        return max_num + 1

    def generate(
        self,
        name: str,
        operations: list["Operation"],
        dependencies: list[str] | None = None,
    ) -> Path:
        """
        Generate a migration file.

        Args:
            name: Short descriptive name for the migration
            operations: List of operations to include
            dependencies: List of migration names this depends on

        Returns:
            Path to the generated migration file
        """
        self.ensure_directory()

        # Determine migration number and full name
        number = self.get_next_number()
        full_name = generate_migration_name(number, name)

        # Generate filename
        filename = f"{full_name}.py"
        filepath = self.migrations_dir / filename

        # Generate content
        content = self._render_migration(
            name=full_name,
            operations=operations,
            dependencies=dependencies or [],
        )

        filepath.write_text(content)
        return filepath

    def _render_migration(
        self,
        name: str,
        operations: list["Operation"],
        dependencies: list[str],
    ) -> str:
        """
        Render migration file content.

        Args:
            name: Migration name
            operations: List of operations
            dependencies: List of dependency names

        Returns:
            Python file content as string
        """
        lines = [
            '"""',
            f"Migration: {name}",
            f"Generated: {datetime.now(timezone.utc).isoformat()}",
            "",
            "Auto-generated migration file. Review before applying.",
            '"""',
            "",
        ]

        # Collect required imports
        op_types = set(type(op).__name__ for op in operations)
        imports = sorted(op_types)

        lines.append("from surreal_orm.migrations import Migration")
        lines.append("from surreal_orm.migrations.operations import (")
        for op_type in imports:
            lines.append(f"    {op_type},")
        lines.append(")")
        lines.append("")
        lines.append("")

        # Migration definition
        lines.append("migration = Migration(")
        lines.append(f'    name="{name}",')
        lines.append(f"    dependencies={dependencies!r},")

        if not operations:
            lines.append("    operations=[],")
        else:
            lines.append("    operations=[")
            for op in operations:
                rendered = self._render_operation(op)
                # Indent the operation
                for line in rendered.split("\n"):
                    lines.append(f"        {line}")
            lines.append("    ],")

        lines.append(")")
        lines.append("")

        return "\n".join(lines)

    def _render_operation(self, op: "Operation") -> str:
        """
        Render a single operation as Python code.

        Args:
            op: Operation to render

        Returns:
            Python code string for the operation
        """
        op_type = type(op).__name__
        args = []

        # Get all dataclass fields
        for field_name in op.__dataclass_fields__:
            if field_name == "reversible":
                continue

            value = getattr(op, field_name)

            # Skip None values for optional fields
            if value is None:
                continue

            # Skip default values
            field_info = op.__dataclass_fields__[field_name]
            if field_info.default is not MISSING and value == field_info.default:
                continue
            if field_info.default_factory is not MISSING:
                default_val = field_info.default_factory()
                if value == default_val:
                    continue

            # Format the value
            args.append(f"{field_name}={self._format_value(value)}")

        # Format as single line if short, multi-line if long
        args_str = ", ".join(args)
        if len(args_str) < 60:
            return f"{op_type}({args_str}),"
        else:
            lines = [f"{op_type}("]
            for arg in args:
                lines.append(f"    {arg},")
            lines.append("),")
            return "\n".join(lines)

    def _format_value(self, value: object) -> str:
        """
        Format a value as Python literal.

        Args:
            value: Value to format

        Returns:
            Python literal string
        """
        if isinstance(value, str):
            # Use repr for proper escaping
            return repr(value)
        elif isinstance(value, bool):
            return str(value)
        elif isinstance(value, (int, float)):
            return str(value)
        elif isinstance(value, list):
            items = [self._format_value(v) for v in value]
            return f"[{', '.join(items)}]"
        elif isinstance(value, dict):
            items = [f"{self._format_value(k)}: {self._format_value(v)}" for k, v in value.items()]
            if len(items) == 0:
                return "{}"
            elif len(items) <= 2:
                return "{" + ", ".join(items) + "}"
            else:
                # Multi-line dict
                lines = ["{"]
                for item in items:
                    lines.append(f"    {item},")
                lines.append("}")
                return "\n".join(lines)
        elif value is None:
            return "None"
        else:
            return repr(value)


def generate_empty_migration(
    migrations_dir: Path | str,
    name: str,
    dependencies: list[str] | None = None,
) -> Path:
    """
    Generate an empty migration file for manual editing.

    Args:
        migrations_dir: Path to migrations directory
        name: Migration name
        dependencies: List of dependencies

    Returns:
        Path to generated file
    """
    generator = MigrationGenerator(migrations_dir)
    return generator.generate(name=name, operations=[], dependencies=dependencies)
