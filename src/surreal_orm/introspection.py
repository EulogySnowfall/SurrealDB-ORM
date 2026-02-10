"""
Public API for database schema introspection.

Provides high-level functions for:
- Generating Python model code from an existing SurrealDB database
- Comparing Python models against the live database schema

Usage::

    from surreal_orm.introspection import generate_models_from_db, schema_diff

    # Generate model code from database
    source = await generate_models_from_db()
    print(source)

    # Compare models against database
    operations = await schema_diff()
    for op in operations:
        print(op.describe())
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from surreal_sdk.connection.http import HTTPConnection

from .migrations.db_introspector import DatabaseIntrospector
from .migrations.model_generator import ModelCodeGenerator

if TYPE_CHECKING:
    from .migrations.operations import Operation
    from .model_base import BaseSurrealModel


async def generate_models_from_db(
    output_path: str | Path | None = None,
    connection: HTTPConnection | None = None,
) -> str:
    """
    Generate Python model code from an existing SurrealDB database.

    Introspects the live database schema and generates Python source
    code containing ``BaseSurrealModel`` subclasses for each table.

    Args:
        output_path: Optional file path to write the generated code.
                     If None, returns the source string without writing.
        connection: Optional HTTP connection. If None, uses the
                    default connection from ``SurrealDBConnectionManager``.

    Returns:
        Generated Python source code as a string.

    Example::

        # Print to stdout
        source = await generate_models_from_db()
        print(source)

        # Write to file
        await generate_models_from_db(output_path="models_generated.py")
    """
    introspector = DatabaseIntrospector(connection=connection)
    state = await introspector.introspect()

    generator = ModelCodeGenerator()
    source = generator.generate(state)

    if output_path is not None:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    return source


async def schema_diff(
    models: list[type[BaseSurrealModel]] | None = None,
    connection: HTTPConnection | None = None,
) -> list[Operation]:
    """
    Compare Python models against the live database schema.

    Introspects both the Python models (forward) and the live database
    (reverse), then computes the operations needed to synchronize them.

    Args:
        models: Optional list of model classes to compare. If None,
                uses all registered models.
        connection: Optional HTTP connection. If None, uses the
                    default connection from ``SurrealDBConnectionManager``.

    Returns:
        List of ``Operation`` instances describing the differences.
        An empty list means the models and database are in sync.

    Example::

        operations = await schema_diff()
        if operations:
            print(f"Found {len(operations)} differences:")
            for op in operations:
                print(f"  - {op.describe()}")
        else:
            print("Models and database are in sync!")
    """
    from .migrations.introspector import ModelIntrospector

    # Forward introspection: Python models → SchemaState
    model_state = ModelIntrospector(models).introspect()

    # Reverse introspection: Live database → SchemaState
    db_introspector = DatabaseIntrospector(connection=connection)
    db_state = await db_introspector.introspect()

    # Compute diff: what operations transform db_state into model_state
    return db_state.diff(model_state)


__all__ = ["generate_models_from_db", "schema_diff"]
