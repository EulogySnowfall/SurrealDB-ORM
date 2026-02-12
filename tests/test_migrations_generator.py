"""
Unit tests for migration file generator.
"""

import shutil
import tempfile
from pathlib import Path

import pytest

from src.surreal_orm.migrations.generator import MigrationGenerator, generate_empty_migration
from src.surreal_orm.migrations.operations import (
    AddField,
    CreateTable,
)


@pytest.fixture
def temp_migrations_dir() -> Path:
    """Create a temporary migrations directory."""
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    shutil.rmtree(temp_dir)


class TestMigrationGenerator:
    """Tests for MigrationGenerator class."""

    def test_ensure_directory_creates_dir(self, temp_migrations_dir: Path) -> None:
        """Test that ensure_directory creates the migrations directory."""
        migrations_dir = temp_migrations_dir / "new_migrations"
        generator = MigrationGenerator(migrations_dir)
        generator.ensure_directory()

        assert migrations_dir.exists()
        assert (migrations_dir / "__init__.py").exists()

    def test_get_next_number_empty_dir(self, temp_migrations_dir: Path) -> None:
        """Test get_next_number with empty directory."""
        generator = MigrationGenerator(temp_migrations_dir)
        assert generator.get_next_number() == 1

    def test_get_next_number_with_existing(self, temp_migrations_dir: Path) -> None:
        """Test get_next_number with existing migrations."""
        generator = MigrationGenerator(temp_migrations_dir)
        generator.ensure_directory()

        # Create some migration files
        (temp_migrations_dir / "0001_initial.py").touch()
        (temp_migrations_dir / "0002_add_users.py").touch()

        assert generator.get_next_number() == 3

    def test_generate_basic_migration(self, temp_migrations_dir: Path) -> None:
        """Test generating a basic migration file."""
        generator = MigrationGenerator(temp_migrations_dir)

        operations = [
            CreateTable(name="users", schema_mode="SCHEMAFULL"),
            AddField(table="users", name="email", field_type="string"),
        ]

        filepath = generator.generate(name="initial", operations=operations)

        assert filepath.exists()
        assert filepath.name == "0001_initial.py"

        content = filepath.read_text()
        assert "Migration: 0001_initial" in content
        assert "from surreal_orm.migrations import Migration" in content
        assert "CreateTable" in content
        assert "AddField" in content

    def test_generate_migration_with_dependencies(self, temp_migrations_dir: Path) -> None:
        """Test generating migration with dependencies."""
        generator = MigrationGenerator(temp_migrations_dir)

        # Create first migration
        generator.generate(name="initial", operations=[CreateTable(name="users")])

        # Create second migration with dependency
        filepath = generator.generate(
            name="add_email",
            operations=[AddField(table="users", name="email", field_type="string")],
            dependencies=["0001_initial"],
        )

        content = filepath.read_text()
        assert "dependencies=['0001_initial']" in content

    def test_generate_sequential_migrations(self, temp_migrations_dir: Path) -> None:
        """Test generating multiple migrations with sequential numbering."""
        generator = MigrationGenerator(temp_migrations_dir)

        path1 = generator.generate(name="first", operations=[])
        path2 = generator.generate(name="second", operations=[])
        path3 = generator.generate(name="third", operations=[])

        assert path1.name == "0001_first.py"
        assert path2.name == "0002_second.py"
        assert path3.name == "0003_third.py"

    def test_generate_migration_content_is_valid_python(self, temp_migrations_dir: Path) -> None:
        """Test that generated migration file is valid Python."""
        generator = MigrationGenerator(temp_migrations_dir)

        operations = [
            CreateTable(name="users", schema_mode="SCHEMAFULL"),
            AddField(table="users", name="email", field_type="string", default="test@example.com"),
        ]

        filepath = generator.generate(name="test_python", operations=operations)

        # Try to compile the file
        content = filepath.read_text()
        try:
            compile(content, filepath, "exec")
        except SyntaxError as e:
            pytest.fail(f"Generated migration is not valid Python: {e}")

    def test_render_operation_simple(self, temp_migrations_dir: Path) -> None:
        """Test rendering simple operation."""
        generator = MigrationGenerator(temp_migrations_dir)

        op = CreateTable(name="users")
        rendered = generator._render_operation(op)

        assert "CreateTable" in rendered
        assert "name='users'" in rendered

    def test_render_operation_with_dict(self, temp_migrations_dir: Path) -> None:
        """Test rendering operation with dict parameter."""
        generator = MigrationGenerator(temp_migrations_dir)

        op = CreateTable(
            name="users",
            permissions={"select": "true", "update": "$auth.id = id"},
        )
        rendered = generator._render_operation(op)

        assert "permissions=" in rendered

    def test_render_operation_with_list(self, temp_migrations_dir: Path) -> None:
        """Test rendering operation with list parameter."""
        generator = MigrationGenerator(temp_migrations_dir)

        from src.surreal_orm.migrations.operations import CreateIndex

        op = CreateIndex(table="users", name="email_idx", fields=["email", "name"])
        rendered = generator._render_operation(op)

        assert "fields=['email', 'name']" in rendered

    def test_format_value_strings(self, temp_migrations_dir: Path) -> None:
        """Test value formatting for strings."""
        generator = MigrationGenerator(temp_migrations_dir)

        assert generator._format_value("test") == "'test'"
        assert generator._format_value("it's a test") == '"it\'s a test"'

    def test_format_value_booleans(self, temp_migrations_dir: Path) -> None:
        """Test value formatting for booleans."""
        generator = MigrationGenerator(temp_migrations_dir)

        assert generator._format_value(True) == "True"
        assert generator._format_value(False) == "False"

    def test_format_value_numbers(self, temp_migrations_dir: Path) -> None:
        """Test value formatting for numbers."""
        generator = MigrationGenerator(temp_migrations_dir)

        assert generator._format_value(42) == "42"
        assert generator._format_value(3.14) == "3.14"


class TestGenerateEmptyMigration:
    """Tests for generate_empty_migration function."""

    def test_generate_empty_migration(self, temp_migrations_dir: Path) -> None:
        """Test generating an empty migration file."""
        filepath = generate_empty_migration(temp_migrations_dir, "manual_changes")

        assert filepath.exists()
        assert "0001_manual_changes.py" in str(filepath)

        content = filepath.read_text()
        assert "operations=[]" in content

    def test_generate_empty_migration_with_dependencies(self, temp_migrations_dir: Path) -> None:
        """Test generating empty migration with dependencies."""
        filepath = generate_empty_migration(
            temp_migrations_dir,
            "depends_on_initial",
            dependencies=["0001_initial"],
        )

        content = filepath.read_text()
        assert "dependencies=['0001_initial']" in content


class TestMigrationFileLoading:
    """Tests for loading generated migration files."""

    def test_generated_migration_is_loadable(self, temp_migrations_dir: Path) -> None:
        """Test that generated migration file can be loaded."""
        generator = MigrationGenerator(temp_migrations_dir)

        operations = [
            CreateTable(name="users", schema_mode="SCHEMAFULL"),
            AddField(table="users", name="email", field_type="string"),
        ]

        filepath = generator.generate(name="loadable", operations=operations)

        # Load the migration
        import importlib.util

        spec = importlib.util.spec_from_file_location("migration", filepath)
        assert spec is not None
        assert spec.loader is not None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        migration = getattr(module, "migration", None)
        assert migration is not None
        # Check it has expected Migration attributes (avoid isinstance due to import path differences)
        assert hasattr(migration, "name")
        assert hasattr(migration, "operations")
        assert migration.name == "0001_loadable"
        assert len(migration.operations) == 2

    def test_loaded_migration_generates_sql(self, temp_migrations_dir: Path) -> None:
        """Test that loaded migration generates correct SQL."""
        generator = MigrationGenerator(temp_migrations_dir)

        operations = [
            CreateTable(name="products", schema_mode="SCHEMAFULL"),
            AddField(table="products", name="name", field_type="string"),
            AddField(table="products", name="price", field_type="float"),
        ]

        filepath = generator.generate(name="products", operations=operations)

        # Load and get SQL
        import importlib.util

        spec = importlib.util.spec_from_file_location("migration", filepath)
        assert spec is not None
        assert spec.loader is not None

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        migration = module.migration
        sql_statements = migration.forwards_sql()

        assert len(sql_statements) == 3
        assert any("DEFINE TABLE products" in sql for sql in sql_statements)
        assert any("DEFINE FIELD name ON products" in sql for sql in sql_statements)
        assert any("DEFINE FIELD price ON products" in sql for sql in sql_statements)
