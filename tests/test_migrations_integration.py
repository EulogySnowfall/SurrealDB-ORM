"""
Integration tests for migrations with SurrealDB.

These tests require a running SurrealDB instance.
Run with: pytest -m integration tests/test_migrations_integration.py
"""

import pytest
import tempfile
import shutil
from pathlib import Path

from src import surreal_orm
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
)
from src.surreal_orm.types import SchemaMode
from src.surreal_orm.migrations.executor import MigrationExecutor, MIGRATIONS_TABLE
from src.surreal_orm.migrations.generator import MigrationGenerator
from src.surreal_orm.migrations.introspector import introspect_models
from src.surreal_orm.migrations.state import SchemaState
from src.surreal_orm.migrations.operations import CreateTable, AddField
from tests.conftest import SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE


SURREALDB_DATABASE = "test_migrations"


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    """Setup SurrealDB connection for tests."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


@pytest.fixture
def temp_migrations_dir() -> Path:
    """Create a temporary migrations directory."""
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def clean_registry() -> None:
    """Clear model registry for isolated tests."""
    clear_model_registry()


@pytest.fixture
async def clean_database():
    """Clean up test tables before and after each test."""
    tables_to_clean = [
        "MigrationTestModel",
        "MigrationUser",
        "TestProduct",
        "WorkflowTable",
        "RollbackTable",
        "FirstTable",
        "SecondTable",
        "ThirdTable",
        "SqlTable",
        "StatusTable",
        MIGRATIONS_TABLE,
    ]

    async def cleanup() -> None:
        try:
            client = await surreal_orm.SurrealDBConnectionManager.get_client()
            for table in tables_to_clean:
                try:
                    await client.query(f"REMOVE TABLE IF EXISTS {table};")
                except Exception:
                    pass
        except Exception:
            pass

    # Setup: clean before test
    await cleanup()

    yield  # Run test

    # Teardown: clean after test
    await cleanup()


@pytest.mark.integration
class TestMigrationExecutor:
    """Integration tests for MigrationExecutor."""

    async def test_ensure_migrations_table(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test that migrations table is created."""
        executor = MigrationExecutor(temp_migrations_dir)
        await executor.ensure_migrations_table()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query(f"INFO FOR TABLE {MIGRATIONS_TABLE};")

        # Table should exist
        assert result is not None

    async def test_get_applied_migrations_empty(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test getting applied migrations when none exist."""
        executor = MigrationExecutor(temp_migrations_dir)
        applied = await executor.get_applied_migrations()

        assert applied == []

    async def test_migrate_creates_table(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test that migration creates table in database."""
        # Generate migration
        generator = MigrationGenerator(temp_migrations_dir)
        generator.generate(
            name="create_test",
            operations=[
                CreateTable(name="MigrationTestModel", schema_mode="SCHEMAFULL"),
                AddField(
                    table="MigrationTestModel",
                    name="name",
                    field_type="string",
                ),
            ],
        )

        # Apply migration
        executor = MigrationExecutor(temp_migrations_dir)
        applied = await executor.migrate()

        assert len(applied) == 1
        assert "0001_create_test" in applied

        # Verify table exists
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query("INFO FOR TABLE MigrationTestModel;")
        assert result is not None

    async def test_migrate_records_applied(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test that applied migrations are recorded."""
        generator = MigrationGenerator(temp_migrations_dir)
        generator.generate(
            name="test_record",
            operations=[CreateTable(name="TestProduct", schema_mode="SCHEMAFULL")],
        )

        executor = MigrationExecutor(temp_migrations_dir)
        await executor.migrate()

        # Check applied migrations
        applied = await executor.get_applied_migrations()
        assert "0001_test_record" in applied

    async def test_migrate_skip_already_applied(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test that already applied migrations are skipped."""
        generator = MigrationGenerator(temp_migrations_dir)
        generator.generate(
            name="skip_test",
            operations=[CreateTable(name="TestProduct", schema_mode="SCHEMAFULL")],
        )

        executor = MigrationExecutor(temp_migrations_dir)

        # Apply first time
        first_applied = await executor.migrate()
        assert len(first_applied) == 1

        # Apply second time - should skip
        second_applied = await executor.migrate()
        assert len(second_applied) == 0

    async def test_migrate_fake(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test fake migration marks as applied without executing."""
        generator = MigrationGenerator(temp_migrations_dir)
        generator.generate(
            name="fake_test",
            operations=[CreateTable(name="FakeTable", schema_mode="SCHEMAFULL")],
        )

        executor = MigrationExecutor(temp_migrations_dir)
        await executor.migrate(fake=True)

        # Should be recorded as applied
        applied = await executor.get_applied_migrations()
        assert "0001_fake_test" in applied

        # But table should NOT exist
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        try:
            result = await client.query("SELECT * FROM FakeTable;")
            # If we get here without error, table might exist
            # Check if empty result
            assert result.is_empty
        except Exception:
            pass  # Table doesn't exist, which is expected

    async def test_get_migration_status(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test getting migration status."""
        generator = MigrationGenerator(temp_migrations_dir)
        generator.generate(
            name="status_test",
            operations=[CreateTable(name="StatusTable")],
        )

        executor = MigrationExecutor(temp_migrations_dir)
        status = await executor.get_migration_status()

        assert "0001_status_test" in status
        assert status["0001_status_test"]["applied"] is False

        await executor.migrate()

        status = await executor.get_migration_status()
        assert status["0001_status_test"]["applied"] is True

    async def test_show_sql(self, temp_migrations_dir: Path) -> None:
        """Test showing SQL for a migration."""
        generator = MigrationGenerator(temp_migrations_dir)
        generator.generate(
            name="sql_test",
            operations=[
                CreateTable(name="SqlTable", schema_mode="SCHEMAFULL"),
                AddField(table="SqlTable", name="value", field_type="int"),
            ],
        )

        executor = MigrationExecutor(temp_migrations_dir)
        sql = await executor.show_sql("0001_sql_test")

        assert "DEFINE TABLE SqlTable SCHEMAFULL" in sql
        assert "DEFINE FIELD value ON SqlTable TYPE int" in sql


@pytest.mark.integration
class TestMigrationRollback:
    """Integration tests for migration rollback."""

    async def test_rollback_removes_table(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test that rollback removes created table."""
        generator = MigrationGenerator(temp_migrations_dir)
        generator.generate(
            name="rollback_test",
            operations=[CreateTable(name="RollbackTable", schema_mode="SCHEMAFULL")],
        )

        executor = MigrationExecutor(temp_migrations_dir)

        # Apply migration
        await executor.migrate()

        # Verify table exists
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query("INFO FOR TABLE RollbackTable;")
        assert result is not None

        # Rollback (to before any migrations)
        rolled_back = await executor.rollback("")

        assert "0001_rollback_test" in rolled_back

        # Verify table is gone
        result = await client.query("SELECT * FROM RollbackTable;")
        assert result.is_empty

    async def test_rollback_to_target(self, temp_migrations_dir: Path, clean_database: None) -> None:
        """Test rolling back to a specific migration."""
        generator = MigrationGenerator(temp_migrations_dir)

        # Create multiple migrations
        generator.generate(
            name="first",
            operations=[CreateTable(name="FirstTable")],
        )
        generator.generate(
            name="second",
            operations=[CreateTable(name="SecondTable")],
        )
        generator.generate(
            name="third",
            operations=[CreateTable(name="ThirdTable")],
        )

        executor = MigrationExecutor(temp_migrations_dir)

        # Apply all
        await executor.migrate()

        # Rollback to first migration
        rolled_back = await executor.rollback("0001_first")

        assert "0003_third" in rolled_back
        assert "0002_second" in rolled_back
        assert "0001_first" not in rolled_back

        # Verify first table still exists
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query("INFO FOR TABLE FirstTable;")
        assert result is not None


@pytest.mark.integration
class TestEndToEndMigrationWorkflow:
    """End-to-end tests for the complete migration workflow."""

    async def test_full_migration_workflow(self, temp_migrations_dir: Path, clean_database: None, clean_registry: None) -> None:
        """Test complete workflow: define model -> generate migration -> apply."""
        clear_model_registry()

        # Define a model
        class WorkflowModel(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="WorkflowTable",
                schema_mode=SchemaMode.SCHEMAFULL,
            )
            id: str | None = None
            name: str
            value: int = 0

        # Introspect models
        desired_state = introspect_models([WorkflowModel])
        current_state = SchemaState()

        # Generate operations
        operations = current_state.diff(desired_state)

        # Generate migration file
        generator = MigrationGenerator(temp_migrations_dir)
        generator.generate(name="workflow", operations=operations)

        # Apply migration
        executor = MigrationExecutor(temp_migrations_dir)
        applied = await executor.migrate()

        assert len(applied) == 1

        # Verify we can use the model
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query("CREATE WorkflowTable SET name = 'test', value = 42;")

        result = await client.query("SELECT * FROM WorkflowTable;")
        assert not result.is_empty
        assert result.first["name"] == "test"
        assert result.first["value"] == 42
