"""
CLI commands for SurrealDB ORM migrations.

Uses click for command-line argument parsing.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

try:
    import click
except ImportError:
    click = None  # type: ignore


def require_click() -> None:
    """Raise error if click is not installed."""
    if click is None:
        print("Error: click is required for CLI. Install with: pip install click")
        sys.exit(1)


def run_async(coro: Any) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


# Only define CLI if click is available
if click is not None:

    @click.group()
    @click.option(
        "--migrations-dir",
        "-m",
        default="migrations",
        help="Migrations directory (default: migrations)",
    )
    @click.option(
        "--url",
        "-u",
        envvar="SURREAL_URL",
        default="http://localhost:8000",
        help="SurrealDB URL",
    )
    @click.option(
        "--namespace",
        "-n",
        envvar="SURREAL_NAMESPACE",
        help="SurrealDB namespace",
    )
    @click.option(
        "--database",
        "-d",
        envvar="SURREAL_DATABASE",
        help="SurrealDB database",
    )
    @click.option(
        "--user",
        envvar="SURREAL_USER",
        default="root",
        help="SurrealDB user",
    )
    @click.option(
        "--password",
        envvar="SURREAL_PASSWORD",
        default="root",
        help="SurrealDB password",
    )
    @click.pass_context
    def cli(
        ctx: click.Context,
        migrations_dir: str,
        url: str,
        namespace: str | None,
        database: str | None,
        user: str,
        password: str,
    ) -> None:
        """SurrealDB ORM migration management tool."""
        ctx.ensure_object(dict)
        ctx.obj["migrations_dir"] = Path(migrations_dir)
        ctx.obj["url"] = url
        ctx.obj["namespace"] = namespace
        ctx.obj["database"] = database
        ctx.obj["user"] = user
        ctx.obj["password"] = password

    @cli.command()
    @click.option("--name", "-n", required=True, help="Migration name")
    @click.option("--empty", is_flag=True, help="Create empty migration for manual editing")
    @click.option("--models", "-m", multiple=True, help="Specific model modules to include")
    @click.pass_context
    def makemigrations(
        ctx: click.Context,
        name: str,
        empty: bool,
        models: tuple[str, ...],
    ) -> None:
        """Generate migration files from model changes."""
        from ..migrations.generator import MigrationGenerator, generate_empty_migration
        from ..migrations.introspector import introspect_models
        from ..migrations.state import SchemaState

        migrations_dir = ctx.obj["migrations_dir"]

        if empty:
            # Create empty migration
            filepath = generate_empty_migration(migrations_dir, name)
            click.echo(f"Created empty migration: {filepath}")
            return

        # Import models if specified
        if models:
            for module_path in models:
                try:
                    __import__(module_path)
                except ImportError as e:
                    click.echo(f"Error importing {module_path}: {e}", err=True)
                    sys.exit(1)

        # Introspect models to get desired state
        desired_state = introspect_models()

        if not desired_state.tables:
            click.echo("No models found. Register models by importing them.")
            sys.exit(1)

        # For now, assume current state is empty (first migration)
        # In a full implementation, we'd load current state from database
        current_state = SchemaState()

        # Compute differences
        operations = current_state.diff(desired_state)

        if not operations:
            click.echo("No changes detected.")
            return

        # Generate migration file
        generator = MigrationGenerator(migrations_dir)
        filepath = generator.generate(name=name, operations=operations)

        click.echo(f"Created migration: {filepath}")
        click.echo(f"Operations: {len(operations)}")
        for op in operations:
            click.echo(f"  - {op.describe()}")

    @cli.command()
    @click.option("--target", "-t", help="Target migration name")
    @click.option("--fake", is_flag=True, help="Mark as applied without executing")
    @click.pass_context
    def migrate(ctx: click.Context, target: str | None, fake: bool) -> None:
        """Apply pending schema migrations."""
        from ..connection_manager import SurrealDBConnectionManager
        from ..migrations.executor import MigrationExecutor

        async def run() -> list[str]:
            # Setup connection
            SurrealDBConnectionManager.set_connection(
                url=ctx.obj["url"],
                user=ctx.obj["user"],
                password=ctx.obj["password"],
                namespace=ctx.obj["namespace"],
                database=ctx.obj["database"],
            )

            executor = MigrationExecutor(ctx.obj["migrations_dir"])
            return await executor.migrate(target=target, fake=fake, schema_only=True)

        try:
            applied = run_async(run())
            if applied:
                click.echo(f"Applied {len(applied)} migration(s):")
                for name in applied:
                    click.echo(f"  - {name}")
            else:
                click.echo("No migrations to apply.")
        except Exception as e:
            click.echo(f"Migration failed: {e}", err=True)
            sys.exit(1)

    @cli.command()
    @click.option("--target", "-t", help="Target migration name")
    @click.pass_context
    def upgrade(ctx: click.Context, target: str | None) -> None:
        """Apply data migrations to transform records."""
        from ..connection_manager import SurrealDBConnectionManager
        from ..migrations.executor import MigrationExecutor

        async def run() -> list[str]:
            SurrealDBConnectionManager.set_connection(
                url=ctx.obj["url"],
                user=ctx.obj["user"],
                password=ctx.obj["password"],
                namespace=ctx.obj["namespace"],
                database=ctx.obj["database"],
            )

            executor = MigrationExecutor(ctx.obj["migrations_dir"])
            return await executor.upgrade(target=target)

        try:
            applied = run_async(run())
            if applied:
                click.echo(f"Applied data migrations for {len(applied)} migration(s):")
                for name in applied:
                    click.echo(f"  - {name}")
            else:
                click.echo("No data migrations to apply.")
        except Exception as e:
            click.echo(f"Upgrade failed: {e}", err=True)
            sys.exit(1)

    @cli.command()
    @click.argument("target")
    @click.pass_context
    def rollback(ctx: click.Context, target: str) -> None:
        """Rollback migrations to TARGET."""
        from ..connection_manager import SurrealDBConnectionManager
        from ..migrations.executor import MigrationExecutor

        async def run() -> list[str]:
            SurrealDBConnectionManager.set_connection(
                url=ctx.obj["url"],
                user=ctx.obj["user"],
                password=ctx.obj["password"],
                namespace=ctx.obj["namespace"],
                database=ctx.obj["database"],
            )

            executor = MigrationExecutor(ctx.obj["migrations_dir"])
            return await executor.rollback(target=target)

        try:
            rolled_back = run_async(run())
            if rolled_back:
                click.echo(f"Rolled back {len(rolled_back)} migration(s):")
                for name in rolled_back:
                    click.echo(f"  - {name}")
            else:
                click.echo("No migrations to rollback.")
        except Exception as e:
            click.echo(f"Rollback failed: {e}", err=True)
            sys.exit(1)

    @cli.command()
    @click.pass_context
    def status(ctx: click.Context) -> None:
        """Show migration status."""
        from ..connection_manager import SurrealDBConnectionManager
        from ..migrations.executor import MigrationExecutor

        async def run() -> dict[str, dict[str, Any]]:
            SurrealDBConnectionManager.set_connection(
                url=ctx.obj["url"],
                user=ctx.obj["user"],
                password=ctx.obj["password"],
                namespace=ctx.obj["namespace"],
                database=ctx.obj["database"],
            )

            executor = MigrationExecutor(ctx.obj["migrations_dir"])
            return await executor.get_migration_status()

        try:
            status_info = run_async(run())

            if not status_info:
                click.echo("No migrations found.")
                return

            click.echo("Migration status:")
            click.echo("-" * 60)

            for name, info in status_info.items():
                status_char = "[X]" if info["applied"] else "[ ]"
                reversible = "R" if info["reversible"] else "-"
                has_data = "D" if info["has_data"] else "-"
                ops = info["operations"]
                click.echo(f"{status_char} {name} ({ops} ops) [{reversible}{has_data}]")

            click.echo("-" * 60)
            click.echo("Legend: [X]=applied, R=reversible, D=has data migrations")
        except Exception as e:
            click.echo(f"Status failed: {e}", err=True)
            sys.exit(1)

    @cli.command()
    @click.argument("migration")
    @click.pass_context
    def sqlmigrate(ctx: click.Context, migration: str) -> None:
        """Show SQL for MIGRATION without executing."""
        from ..migrations.executor import MigrationExecutor

        executor = MigrationExecutor(ctx.obj["migrations_dir"])

        async def run() -> str:
            return await executor.show_sql(migration)

        try:
            sql = run_async(run())
            click.echo(sql)
        except FileNotFoundError:
            click.echo(f"Migration not found: {migration}", err=True)
            sys.exit(1)
        except Exception as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    @cli.command()
    @click.pass_context
    def shell(ctx: click.Context) -> None:
        """Start an interactive SurrealDB shell."""
        from ..connection_manager import SurrealDBConnectionManager

        async def setup() -> None:
            SurrealDBConnectionManager.set_connection(
                url=ctx.obj["url"],
                user=ctx.obj["user"],
                password=ctx.obj["password"],
                namespace=ctx.obj["namespace"],
                database=ctx.obj["database"],
            )
            client = await SurrealDBConnectionManager.get_client()
            click.echo(f"Connected to {ctx.obj['url']}")
            click.echo(f"Namespace: {ctx.obj['namespace']}, Database: {ctx.obj['database']}")
            click.echo("Type 'exit' or 'quit' to exit. Enter SurrealQL queries:")
            click.echo("-" * 60)

            while True:
                try:
                    query = click.prompt("surreal", prompt_suffix="> ")
                    if query.lower() in ("exit", "quit"):
                        break
                    if not query.strip():
                        continue

                    result = await client.query(query)
                    if result.is_empty:  # type: ignore[attr-defined]
                        click.echo("(empty result)")
                    else:
                        for record in result.all_records:  # type: ignore[attr-defined]
                            click.echo(record)
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    click.echo(f"Error: {e}")

            click.echo("Goodbye!")

        run_async(setup())

else:
    # Placeholder CLI if click is not installed
    def cli() -> None:  # type: ignore
        """CLI placeholder when click is not installed."""
        require_click()


def main() -> None:
    """Entry point for the CLI."""
    require_click()
    cli()


if __name__ == "__main__":
    main()
