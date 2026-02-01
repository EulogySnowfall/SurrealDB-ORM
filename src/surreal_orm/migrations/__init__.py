"""
SurrealDB ORM Migration System.

This module provides Django-style migrations for SurrealDB, including:
- Schema versioning with migration files
- Auto-detection of model changes
- Forward and backward migrations
- Data migrations for record transformations

Usage:
    # Generate migrations from model changes
    await manager.makemigrations(name="initial")

    # Apply pending migrations
    await manager.migrate()

    # Rollback to a specific migration
    await manager.rollback("0001_initial")
"""

from .migration import Migration
from .operations import (
    AddField,
    AlterField,
    CreateIndex,
    CreateTable,
    DataMigration,
    DefineAccess,
    DropField,
    DropIndex,
    DropTable,
    Operation,
    RawSQL,
    RemoveAccess,
)

__all__ = [
    "Migration",
    "Operation",
    "CreateTable",
    "DropTable",
    "AddField",
    "DropField",
    "AlterField",
    "CreateIndex",
    "DropIndex",
    "DefineAccess",
    "RemoveAccess",
    "DataMigration",
    "RawSQL",
]
