"""
Record references field type for SurrealDB ORM (SurrealDB 3.0+).

Maps to SurrealDB's ``REFERENCE`` clause on ``DEFINE FIELD``.  In SurrealDB 3.0
``REFERENCE`` is **not** a type — it is a modifier applied to fields of type
``record<T>`` or ``array<record<T>>``.  The generated migration output is::

    DEFINE FIELD books ON author TYPE option<array<record<books>>> REFERENCE;
    DEFINE FIELD owner ON license TYPE record<person> REFERENCE ON DELETE CASCADE;

Usage::

    from surreal_orm.fields import ReferencesField

    class Author(BaseSurrealModel):
        name: str
        books: ReferencesField["books"]
        # → DEFINE FIELD books ON author TYPE option<array<record<books>>> REFERENCE;

    class License(BaseSurrealModel):
        owner: ReferencesField["person", "CASCADE"]
        # → DEFINE FIELD owner ON license TYPE option<record<person>> REFERENCE ON DELETE CASCADE;
"""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema


class _ReferencesMarker:
    """
    Pydantic-compatible marker for fields with the ``REFERENCE`` clause.

    Stored inside ``Annotated[list[str] | None, _ReferencesMarker(table, on_delete)]``
    to carry the referenced table name and optional ON DELETE strategy.
    """

    table: str
    on_delete: str | None

    def __init__(self, table: str = "", on_delete: str | None = None) -> None:
        self.table = table.lower()
        self.on_delete = on_delete.upper() if on_delete else None

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
    Reference-tracking field for SurrealDB 3.0's ``REFERENCE`` clause.

    ``ReferencesField["books"]`` resolves to
    ``Annotated[list[str] | None, _ReferencesMarker("books")]``.

    Optionally specify an ON DELETE strategy as a second parameter::

        ReferencesField["books", "CASCADE"]   # ON DELETE CASCADE
        ReferencesField["books", "REJECT"]    # ON DELETE REJECT
        ReferencesField["books", "UNSET"]     # ON DELETE UNSET
        ReferencesField["books"]              # ON DELETE IGNORE (default)

    The generated migration uses the ``REFERENCE`` clause (not a separate type)::

        DEFINE FIELD books ON author TYPE option<array<record<books>>> REFERENCE;
        DEFINE FIELD books ON author TYPE option<array<record<books>>> REFERENCE ON DELETE CASCADE;

    Example::

        class Author(BaseSurrealModel):
            name: str
            books: ReferencesField["books"]

        class Post(BaseSurrealModel):
            author: ReferencesField["users", "CASCADE"]
    """

    def __class_getitem__(cls, params: str | tuple[str, str]) -> type:
        """``ReferencesField["books"]`` or ``ReferencesField["books", "CASCADE"]``."""
        if isinstance(params, tuple):
            table, on_delete = params
        else:
            table = params
            on_delete = None
        return Annotated[list[str] | None, _ReferencesMarker(table, on_delete)]  # type: ignore[return-value]


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


def get_references_on_delete(field_type: Any) -> str | None:
    """
    Extract the ON DELETE strategy from a references field type.

    Returns:
        Strategy string (e.g., ``"CASCADE"``, ``"REJECT"``) or None.
    """
    marker = _get_references_marker(field_type)
    if marker is not None:
        return marker.on_delete
    return None


__all__ = [
    "ReferencesField",
    "get_references_info",
    "get_references_on_delete",
    "is_references_field",
]
