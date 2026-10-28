"""
Unit tests for CLI commands.

Uses Click's CliRunner for testing CLI commands without database connections.
"""

import asyncio
import dataclasses
import shutil
import tempfile
import threading
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

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
        """Test makemigrations detects model changes.

        Uses ``--no-from-db`` because this is a unit test with no database: since
        #171 the default reads the current schema from the live server, and the
        point here is only that a model turns into operations.
        """
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
                "--no-from-db",
            ],
        )
        assert result.exit_code == 0
        assert "Created migration" in result.output
        assert "Operations" in result.output

        # Clean up
        clear_model_registry()


class TestMakeMigrationsAgainstDatabase:
    """#171/#185 — makemigrations diffed against an empty SchemaState.

    It re-emitted every table on every run. The current state now comes from the
    live database, so an already-migrated schema produces no operations.
    """

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_an_already_migrated_schema_produces_no_operations(
        self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path
    ) -> None:
        """A table the database already has is not re-emitted."""
        from src.surreal_orm.migrations.introspector import introspect_models
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class MigratedUser(BaseSurrealModel):
            id: str | None = None
            name: str

        # The database reports exactly what the models describe.
        mock_run_async.side_effect = mock_run_async_factory(introspect_models())

        result = runner.invoke(
            cli_command,
            ["--migrations-dir", str(temp_migrations_dir), "makemigrations", "--name", "noop"],
        )

        clear_model_registry()
        assert result.exit_code == 0
        assert "No changes detected" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_a_new_model_is_still_emitted(
        self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path
    ) -> None:
        """A model the database does not have still generates a migration."""
        from src.surreal_orm.migrations.state import SchemaState
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class BrandNewThing(BaseSurrealModel):
            id: str | None = None
            label: str

        mock_run_async.side_effect = mock_run_async_factory(SchemaState())  # empty database

        result = runner.invoke(
            cli_command,
            ["--migrations-dir", str(temp_migrations_dir), "makemigrations", "--name", "create_thing"],
        )

        clear_model_registry()
        assert result.exit_code == 0
        assert "Created migration" in result.output

    def test_no_from_db_skips_the_database_entirely(self, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """``--no-from-db`` restores the empty-state behaviour, with no connection.

        This is the escape hatch for a true first migration and for offline use —
        Django does not need a database to generate migrations either.
        """
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class OfflineModel(BaseSurrealModel):
            id: str | None = None
            title: str

        # No run_async patch: reaching the database at all would fail here.
        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "makemigrations",
                "--name",
                "offline",
                "--no-from-db",
            ],
        )

        clear_model_registry()
        assert result.exit_code == 0
        assert "Created migration" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_an_unreachable_database_fails_loudly_and_names_the_escape_hatch(
        self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path
    ) -> None:
        """Falling back to an empty schema silently would be the bug itself."""
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class UnreachableModel(BaseSurrealModel):
            id: str | None = None
            name: str

        mock_run_async.side_effect = mock_run_async_factory(exception=ConnectionError("refused"))

        result = runner.invoke(
            cli_command,
            ["--migrations-dir", str(temp_migrations_dir), "makemigrations", "--name", "boom"],
        )

        clear_model_registry()
        assert result.exit_code != 0
        assert "--no-from-db" in result.output
        assert "Created migration" not in result.output


class TestMakeMigrationsDropMissing:
    """Which irreversible operations `makemigrations` is willing to write.

    See ``split_destructive`` for why a database object with no model is not
    necessarily obsolete.
    """

    @staticmethod
    def _db_state(*names: str):
        from tests.test_makemigrations_drop_unit import _db_with

        return _db_with(*names)

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_an_unmodelled_table_is_not_dropped_by_default(
        self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path
    ) -> None:
        """Silently writing an irreversible REMOVE TABLE is the wrong default."""
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class KeptModel(BaseSurrealModel):
            id: str | None = None
            name: str

        mock_run_async.side_effect = mock_run_async_factory(self._db_state("legacy_audit"))

        result = runner.invoke(
            cli_command,
            ["--migrations-dir", str(temp_migrations_dir), "makemigrations", "--name", "x"],
        )

        clear_model_registry()
        assert result.exit_code == 0, result.output
        written = "".join(p.read_text() for p in temp_migrations_dir.glob("0001_*.py"))
        assert "DropTable" not in written, written

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_skipped_tables_are_reported_not_hidden(
        self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path
    ) -> None:
        """A genuinely obsolete table must not vanish from view."""
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class KeptModel2(BaseSurrealModel):
            id: str | None = None
            name: str

        mock_run_async.side_effect = mock_run_async_factory(self._db_state("legacy_audit"))

        result = runner.invoke(
            cli_command,
            ["--migrations-dir", str(temp_migrations_dir), "makemigrations", "--name", "x"],
        )

        clear_model_registry()
        assert result.exit_code == 0, result.output
        assert "legacy_audit" in result.output
        assert "--drop-missing" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_drop_missing_opts_in(self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """The flag is how you ask for the destructive operation."""
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class KeptModel3(BaseSurrealModel):
            id: str | None = None
            name: str

        mock_run_async.side_effect = mock_run_async_factory(self._db_state("legacy_audit"))

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "makemigrations",
                "--name",
                "x",
                "--drop-missing",
            ],
        )

        clear_model_registry()
        assert result.exit_code == 0, result.output
        assert "Drop table legacy_audit" in result.output

    @patch("src.surreal_orm.cli.commands.run_async")
    def test_the_migrations_table_is_never_dropped_even_with_the_flag(
        self, mock_run_async, runner: "CliRunner", cli_command, temp_migrations_dir: Path
    ) -> None:
        """No flag makes erasing the migration history a reasonable request."""
        from src.surreal_orm.migrations.executor import MIGRATIONS_TABLE
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class KeptModel4(BaseSurrealModel):
            id: str | None = None
            name: str

        mock_run_async.side_effect = mock_run_async_factory(self._db_state(MIGRATIONS_TABLE))

        result = runner.invoke(
            cli_command,
            [
                "--migrations-dir",
                str(temp_migrations_dir),
                "makemigrations",
                "--name",
                "x",
                "--drop-missing",
            ],
        )

        clear_model_registry()
        assert result.exit_code == 0, result.output
        assert MIGRATIONS_TABLE not in result.output
        written = "".join(p.read_text() for p in temp_migrations_dir.glob("0001_*.py"))
        assert MIGRATIONS_TABLE not in written, written


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


# =============================================================================
# Issue #303 — run_async needs no current event loop
# =============================================================================
#
# Every test above patches run_async, so the real helper never ran in CI. It
# looked up the thread's current loop: Python 3.12-3.13 created one with a
# DeprecationWarning, 3.14+ raises RuntimeError, and a click command starts
# with no loop, so every database command failed on 3.14+.


async def _i303_answer() -> int:
    """Suspend once on the loop, then return a known value."""
    await asyncio.sleep(0)
    return 42


async def _i303_running_loop() -> asyncio.AbstractEventLoop:
    """Return the loop this coroutine runs on."""
    await asyncio.sleep(0)
    return asyncio.get_running_loop()


def _i303_in_fresh_thread(fn: Callable[[], Any]) -> Any:
    """Call *fn* in a new thread with ``DeprecationWarning`` raised as an error.

    A new thread has no current event loop, the state a click command starts
    in. The warning filter is set inside the thread so that it applies there
    whether or not the interpreter keeps warning filters per context.
    """
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", DeprecationWarning)
                outcome["value"] = fn()
        except BaseException as exc:  # re-raised in the test's thread below
            outcome["error"] = exc

    thread = threading.Thread(target=target, name="i303-no-loop")
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive(), "run_async did not return"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _i303_loop_running() -> bool:
    """Whether the calling code runs inside an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture
def i303_restore_default_connection() -> Iterator[None]:
    """Put back the "default" connection a command under test configured.

    The commands below really run their coroutine, which calls
    ``set_connection()`` on the process-wide manager.
    """
    from src.surreal_orm.connection_manager import SurrealDBConnectionManager

    saved = SurrealDBConnectionManager.get_config("default")
    yield
    if saved is None:
        SurrealDBConnectionManager.unset_connection_sync()
    else:
        SurrealDBConnectionManager.add_connection("default", **dataclasses.asdict(saved))


class TestI303RunAsync:
    """The real run_async, called the way click calls a command."""

    def test_returns_the_result_in_a_thread_with_no_current_loop(self) -> None:
        """No current loop and warnings as errors: the coroutine still runs."""
        from src.surreal_orm.cli.commands import run_async

        assert _i303_in_fresh_thread(lambda: run_async(_i303_answer())) == 42

    def test_two_calls_in_a_row_each_run_on_a_loop_closed_afterwards(self) -> None:
        """A second call works, and neither leaves a loop open or set."""
        import asyncio.events

        from src.surreal_orm.cli.commands import run_async

        with patch.object(asyncio.events, "set_event_loop", wraps=asyncio.events.set_event_loop) as set_loop:
            first, second = _i303_in_fresh_thread(lambda: (run_async(_i303_running_loop()), run_async(_i303_running_loop())))

        assert first is not second
        assert first.is_closed()
        assert second.is_closed()
        # A loop installed as the thread's current one is reset to None.
        assert set_loop.call_args_list[-1:] in ([], [call(None)])

    def test_an_exception_raised_by_the_coroutine_reaches_the_caller(self) -> None:
        """Commands report failures through their ``except Exception``."""
        from src.surreal_orm.cli.commands import run_async

        async def fail() -> None:
            await asyncio.sleep(0)
            raise ConnectionError("refused")

        with pytest.raises(ConnectionError, match="refused"):
            _i303_in_fresh_thread(lambda: run_async(fail()))


@pytest.mark.filterwarnings("error::DeprecationWarning")
@pytest.mark.usefixtures("i303_restore_default_connection")
class TestI303CommandsEndToEnd:
    """Commands through CliRunner with the real run_async; only the database layer is mocked."""

    def test_status_twice_in_one_process(self, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """Each invocation gets its own loop, closed when the command returns."""
        loops: list[asyncio.AbstractEventLoop] = []

        class I303Executor:
            def __init__(self, migrations_dir: Path) -> None:
                self.migrations_dir = migrations_dir

            async def get_migration_status(self) -> dict[str, dict[str, Any]]:
                loops.append(asyncio.get_running_loop())
                await asyncio.sleep(0)
                return {"0001_initial": {"applied": True, "reversible": True, "has_data": False, "operations": 2}}

        args = ["--migrations-dir", str(temp_migrations_dir), "-n", "i303", "-d", "i303", "status"]
        with patch("src.surreal_orm.migrations.executor.MigrationExecutor", I303Executor):
            results = [runner.invoke(cli_command, args), runner.invoke(cli_command, args)]

        for result in results:
            assert result.exit_code == 0, result.output
            assert "[X] 0001_initial (2 ops)" in result.output
        assert len(loops) == 2
        assert loops[0] is not loops[1]
        assert all(loop.is_closed() for loop in loops)

    def test_makemigrations_reads_the_schema(self, runner: "CliRunner", cli_command, temp_migrations_dir: Path) -> None:
        """The default ``--from-db`` path awaits the introspector on a real loop."""
        from src.surreal_orm.migrations.introspector import introspect_models
        from src.surreal_orm.model_base import BaseSurrealModel, clear_model_registry

        clear_model_registry()

        class I303Migrated(BaseSurrealModel):
            id: str | None = None
            name: str

        introspector = MagicMock()
        introspector.return_value.introspect = AsyncMock(return_value=introspect_models())
        try:
            with patch("src.surreal_orm.migrations.db_introspector.DatabaseIntrospector", introspector):
                result = runner.invoke(
                    cli_command,
                    [
                        "--migrations-dir",
                        str(temp_migrations_dir),
                        "-n",
                        "i303",
                        "-d",
                        "i303",
                        "makemigrations",
                        "--name",
                        "noop",
                    ],
                )
        finally:
            clear_model_registry()

        assert result.exit_code == 0, result.output
        assert "No changes detected" in result.output
        introspector.return_value.introspect.assert_awaited_once()

    def test_shell_prompts_outside_the_event_loop(self, runner: "CliRunner", cli_command) -> None:
        """Queries run on the loop, the blocking prompt never does.

        While a coroutine runs, asyncio turns Ctrl+C into a cancellation of that
        coroutine, which a blocking call inside it (the prompt) only sees at its
        next ``await``: Ctrl+C at the prompt would do nothing visible and then
        abort the shell on the next query.
        """
        import click

        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        prompted_inside_loop: list[bool] = []
        real_prompt = click.prompt

        def prompt(*args: Any, **kwargs: Any) -> Any:
            prompted_inside_loop.append(_i303_loop_running())
            return real_prompt(*args, **kwargs)

        response = MagicMock(is_empty=False, all_records=[{"answer": 42}])
        client = MagicMock()
        client.query = AsyncMock(return_value=response)

        with (
            patch.object(SurrealDBConnectionManager, "get_client", new=AsyncMock(return_value=client)),
            patch.object(click, "prompt", side_effect=prompt),
        ):
            result = runner.invoke(cli_command, ["-n", "i303", "-d", "i303", "shell"], input="RETURN 42;\nexit\n")

        assert result.exit_code == 0, result.output
        assert "{'answer': 42}" in result.output
        assert "Goodbye!" in result.output
        client.query.assert_awaited_once_with("RETURN 42;")
        assert prompted_inside_loop == [False, False]
