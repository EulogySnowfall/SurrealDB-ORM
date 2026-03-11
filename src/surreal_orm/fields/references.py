"""
Record references field type for SurrealDB ORM (SurrealDB 3.0+).

Provides ``ReferencesField`` that maps to SurrealDB's ``references<record<T>>``
type — a back-reference that automatically tracks which records of type *T*
point to the current record.

Usage::

    from surreal_orm.fields import ReferencesField

    class Author(BaseSurrealModel):
        name: str
        # Automatically populated with record IDs from books.author
        books: ReferencesField["books"]
"""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema


class _ReferencesMarker:
    """
    Pydantic-compatible marker for ``references<record<T>>`` fields.

    Stored inside ``Annotated[list[str] | None, _ReferencesMarker(table)]``
    to carry the referenced table name.
    """

    table: str

    def __init__(self, table: str = "") -> None:
        self.table = table.lower()

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build a nullable list[str] schema with a default of None."""
        inner_schema = handler.generate_schema(list[str])
        return core_schema.with_default_schema(
            core_schema.nullable_schema(inner_schema),
            default=None,
        )

    def __get_pydantic_json_schema__(
        self,
        _schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Generate JSON schema matching the nullable inner type and default."""
        json_schema = handler(_schema)
        if isinstance(json_schema, dict) and "default" not in json_schema:
            json_schema["default"] = None
        return json_schema


class ReferencesField:
    """
    Back-reference field for SurrealDB 3.0's ``references<record<T>>`` type.

    ``ReferencesField["books"]`` resolves to
    ``Annotated[list[str] | None, _ReferencesMarker("books")]``.

    The field is read-only at the ORM level — SurrealDB automatically
    populates it with record IDs that reference the current record.

    Example::

        class Author(BaseSurrealModel):
            name: str
            books: ReferencesField["books"]
    """

    def __class_getitem__(cls, table: str) -> type:
        """``ReferencesField["books"]`` → ``Annotated[...]``."""
        return Annotated[list[str] | None, _ReferencesMarker(table)]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _get_references_marker(annotation: Any) -> _ReferencesMarker | None:
    """Extract the ``_ReferencesMarker`` from a type annotation."""
    origin = get_origin(annotation)
    if origin is Annotated:
        for arg in get_args(annotation):
            if isinstance(arg, _ReferencesMarker):
                return arg
    return None


def is_references_field(field_type: Any) -> bool:
    """Check if a field type is a ReferencesField type."""
    return _get_references_marker(field_type) is not None


def get_references_info(field_type: Any) -> str | None:
    """
    Extract the referenced table name from a references field type.

    Returns:
        Table name string (e.g., ``"books"``) or None.
    """
    marker = _get_references_marker(field_type)
    if marker is not None:
        return marker.table
    return None


__all__ = [
    "ReferencesField",
    "get_references_info",
    "is_references_field",
]
