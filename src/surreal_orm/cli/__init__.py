"""
SurrealDB ORM Command Line Interface.

Provides Django-style migration commands:
- makemigrations: Generate migration files from model changes
- migrate: Apply schema migrations to the database
- upgrade: Apply data migrations to transform records
- rollback: Revert migrations to a specific point
- status: Show migration status
- sqlmigrate: Show SQL for a migration without executing
"""

from .commands import cli

__all__ = ["cli"]
