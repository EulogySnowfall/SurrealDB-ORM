"""
Unit tests for CLI commands.

Uses Click's CliRunner for testing CLI commands without database connections.
"""

import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


def mock_run_async_factory(return_value: Any = None, exception: Exception | None = None):
    """
    Create a mock side_effect for run_async that properly closes the coroutine.

    When mocking run_async, the coroutine passed to it must be closed to avoid
    'coroutine was never awaited' warnings.

    Args:
        return_value: Value to return from the mock
        exception: Exception to raise instead of returning
    """

    def side_effect(coro):
        coro.close()
        if exception is not None:
            raise exception
        return return_value

    return side_effect


# Check if click is available
try:
    from click.testing import CliRunner

    click_available = True
except ImportError:
    click_available = False


pytestmark = pytest.mark.skipif(not click_available, reason="click not installed")


@pytest.fixture
def runner() -> "CliRunner":
    """Create a CliRunner for testing."""
    from click.testing import CliRunner

    return CliRunner()


@pytest.fixture
def temp_migrations_dir() -> Path:
    """Create a temporary migrations directory."""
    temp_dir = Path(tempfile.mkdtemp())
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def cli_command():
    """Import CLI command."""
    from src.surreal_orm.cli.commands import cli

    return cli


class TestCliBasics:
    """Tests for basic CLI functionality."""

    def test_cli_help(self, runner: "CliRunner", cli_command) -> None:
        """Test CLI --help shows usage."""
        result = runner.invoke(cli_command, ["--help"])
        assert result.exit_code == 0
        assert "SurrealDB ORM migration management tool" in result.output

    def test_cli_version_options(self, runner: "CliRunner", cli_command) -> None:
        """Test CLI accepts connection options."""
        result = runner.invoke(
            cli_command,
            ["--url", "http://localhost:8000", "--namespace", "test", "--database", "test", "--help"],
        )
        assert result.exit_code == 0


class TestMakeMigrationsCommand:
    """Tests for makemigrations command."""

    def test_makemigrations_help(self, runner: "CliRunner", cli_command) -> None:
        """Test makemigrations --help."""
        result = runner.invoke(cli_command, ["makemigrations", "--help"])
        assert result.exit_code == 0
        assert "Generate migration files" in result.output

    def test_makemigrations_empty(self, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test creating empty migration."""
        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "makemigrations",
                "--name",
                "test_migration",
                "--empty",
            ],
        )
        assert result.exit_code == 0
        assert "Created empty migration" in result.output
        # Verify file was created
        migration_files = list(temp_migrations_dir.glob("*.py"))
        assert len(migration_files) >= 1  # At least __init__.py and migration file

    def test_makemigrations_requires_name(self, runner: "CliRunner", cli_command) -> None:
        """Test that makemigrations requires --name."""
        result = runner.invoke(cli_command, ["makemigrations"])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_makemigrations_no_models(self, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test makemigrations with no models registered."""
        # Clear model registry
        from src.surreal_orm.model_base import clear_model_registry

        clear_model_registry()

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "makemigrations",
                "--name",
                "initial",
            ],
        )
        # Should fail or warn about no models
        assert "No models found" in result.output or result.exit_code != 0

    def test_makemigrations_with_model(self, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test makemigrations detects model changes."""
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        # Define a model
        class TestUser(BaseSurrealModel):
            id: str | None = None
            name: str
            email: str

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "makemigrations",
                "--name",
                "create_user",
            ],
        )
        assert result.exit_code == 0
        assert "Created migration" in result.output
        assert "Operations" in result.output

        # Clean up
        clear_model_registry()


class TestMigrateCommand:
    """Tests for migrate command."""

    def test_migrate_help(self, runner: "CliRunner", cli_command) -> None:
        """Test migrate --help."""
        result = runner.invoke(cli_command, ["migrate", "--help"])
        assert result.exit_code == 0
        assert "Apply pending schema migrations" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_migrate_no_migrations(self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test migrate with no pending migrations."""
        mock_run_async.side_effect = mock_run_async_factory([])

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "--namespace",
                "test",
                "--database",
                "test",
                "migrate",
            ],
        )
        # Should handle gracefully
        assert "No migrations to apply" in result.output or result.exit_code == 0

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_migrate_applies_migrations(
        self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path
    ) -> None:
        """Test migrate applies pending migrations."""
        mock_run_async.side_effect = mock_run_async_factory(["0001_initial", "0002_add_field"])

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "--namespace",
                "test",
                "--database",
                "test",
                "migrate",
            ],
        )
        assert result.exit_code == 0
        assert "Applied 2 migration" in result.output
        assert "0001_initial" in result.output
        assert "0002_add_field" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_migrate_with_fake(self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test migrate --fake option."""
        mock_run_async.side_effect = mock_run_async_factory(["0001_initial"])

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "--namespace",
                "test",
                "--database",
                "test",
                "migrate",
                "--fake",
            ],
        )
        assert result.exit_code == 0


class TestUpgradeCommand:
    """Tests for upgrade command."""

    def test_upgrade_help(self, runner: "CliRunner", cli_command) -> None:
        """Test upgrade --help."""
        result = runner.invoke(cli_command, ["upgrade", "--help"])
        assert result.exit_code == 0
        assert "Apply data migrations" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_upgrade_applies_data_migrations(
        self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path
    ) -> None:
        """Test upgrade applies data migrations."""
        mock_run_async.side_effect = mock_run_async_factory(["0001_initial"])

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "--namespace",
                "test",
                "--database",
                "test",
                "upgrade",
            ],
        )
        assert result.exit_code == 0


class TestRollbackCommand:
    """Tests for rollback command."""

    def test_rollback_help(self, runner: "CliRunner", cli_command) -> None:
        """Test rollback --help."""
        result = runner.invoke(cli_command, ["rollback", "--help"])
        assert result.exit_code == 0
        assert "Rollback migrations" in result.output

    def test_rollback_requires_target(self, runner: "CliRunner", cli_command) -> None:
        """Test that rollback requires target argument."""
        result = runner.invoke(cli_command, ["rollback"])
        assert result.exit_code != 0
        assert "Missing argument" in result.output or "TARGET" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_rollback_to_target(self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test rollback to specific target."""
        mock_run_async.side_effect = mock_run_async_factory(["0003_third", "0002_second"])

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "--namespace",
                "test",
                "--database",
                "test",
                "rollback",
                "0001_initial",
            ],
        )
        assert result.exit_code == 0
        assert "Rolled back 2 migration" in result.output


class TestStatusCommand:
    """Tests for status command."""

    def test_status_help(self, runner: "CliRunner", cli_command) -> None:
        """Test status --help."""
        result = runner.invoke(cli_command, ["status", "--help"])
        assert result.exit_code == 0
        assert "Show migration status" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_status_no_migrations(self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test status with no migrations."""
        mock_run_async.side_effect = mock_run_async_factory({})

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "--namespace",
                "test",
                "--database",
                "test",
                "status",
            ],
        )
        assert "No migrations found" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_status_shows_migrations(self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test status shows migration list."""
        mock_run_async.side_effect = mock_run_async_factory(
            {
                "0001_initial": {"applied": True, "reversible": True, "has_data": False, "operations": 3},
                "0002_add_field": {"applied": False, "reversible": True, "has_data": True, "operations": 1},
            }
        )

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "--namespace",
                "test",
                "--database",
                "test",
                "status",
            ],
        )
        assert result.exit_code == 0
        assert "Migration status" in result.output
        assert "0001_initial" in result.output
        assert "0002_add_field" in result.output
        assert "[X]" in result.output  # Applied marker
        assert "[ ]" in result.output  # Not applied marker


class TestSqlMigrateCommand:
    """Tests for sqlmigrate command."""

    def test_sqlmigrate_help(self, runner: "CliRunner", cli_command) -> None:
        """Test sqlmigrate --help."""
        result = runner.invoke(cli_command, ["sqlmigrate", "--help"])
        assert result.exit_code == 0
        assert "Show SQL for MIGRATION" in result.output

    def test_sqlmigrate_requires_migration(self, runner: "CliRunner", cli_command) -> None:
        """Test that sqlmigrate requires migration argument."""
        result = runner.invoke(cli_command, ["sqlmigrate"])
        assert result.exit_code != 0

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_sqlmigrate_shows_sql(self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test sqlmigrate shows SQL."""
        mock_run_async.side_effect = mock_run_async_factory(
            "DEFINE TABLE users SCHEMAFULL;\nDEFINE FIELD name ON users TYPE string;"
        )

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "sqlmigrate",
                "0001_initial",
            ],
        )
        assert result.exit_code == 0
        assert "DEFINE TABLE" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_sqlmigrate_not_found(self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Test sqlmigrate with non-existent migration."""
        mock_run_async.side_effect = mock_run_async_factory(exception=FileNotFoundError("Migration not found"))

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "sqlmigrate",
                "nonexistent",
            ],
        )
        assert result.exit_code != 0
        assert "not found" in result.output.lower()


class TestEnvironmentVariables:
    """Tests for environment variable support."""

    def test_url_from_env(self, runner: "CliRunner", cli_command) -> None:
        """Test SURREAL_URL environment variable."""
        result = runner.invoke(
            cli_command,
            ["--help"],
            env={"SURREAL_URL": "http://custom:9000"},
        )
        assert result.exit_code == 0

    def test_namespace_from_env(self, runner: "CliRunner", cli_command) -> None:
        """Test SURREAL_NAMESPACE environment variable."""
        result = runner.invoke(
            cli_command,
            ["--help"],
            env={"SURREAL_NAMESPACE": "myns"},
        )
        assert result.exit_code == 0

    def test_database_from_env(self, runner: "CliRunner", cli_command) -> None:
        """Test SURREAL_DATABASE environment variable."""
        result = runner.invoke(
            cli_command,
            ["--help"],
            env={"SURREAL_DATABASE": "mydb"},
        )
        assert result.exit_code == 0
