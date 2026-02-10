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

from .db_introspector import DatabaseIntrospector
from .define_parser import (
    parse_define_access,
    parse_define_field,
    parse_define_index,
    parse_define_table,
)
from .migration import Migration
from .model_generator import ModelCodeGenerator
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
    # Introspection
    "DatabaseIntrospector",
    "ModelCodeGenerator",
    "parse_define_field",
    "parse_define_table",
    "parse_define_index",
    "parse_define_access",
    # Migrations
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
