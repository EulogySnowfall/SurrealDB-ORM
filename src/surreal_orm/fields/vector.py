"""
Vector field type for SurrealDB HNSW vector indexes.

This module provides the VectorField type that generates
``array<float>`` fields with dimension and storage-type metadata,
enabling the ORM to auto-create HNSW indexes via migrations.

Usage::

    from surreal_orm.fields import VectorField

    class Document(BaseSurrealModel):
        title: str
        embedding: VectorField[1536]            # 1536-dim, default F32
        small_vec: VectorField[384, "F64"]      # 384-dim, F64 storage
"""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema


class _VectorMarker:
    """
    Pydantic-compatible marker for vector fields.

    Stored inside ``Annotated[list[float] | None, _VectorMarker(dim, type)]``
    to carry dimension and storage-type metadata.
    """

    dimension: int
    vector_type: str

    def __init__(self, dimension: int = 0, vector_type: str = "F32") -> None:
        self.dimension = dimension
        self.vector_type = vector_type

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build a nullable list[float] schema with a default of None."""
        inner_schema = handler.generate_schema(list[float])
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


class VectorField:
    """
    Vector field for SurrealDB HNSW vector indexes.

    Dual-use class:

    - ``VectorField[dimension]`` — type annotation that resolves to
      ``Annotated[list[float] | None, _VectorMarker(dimension, "F32")]``
    - ``VectorField[dimension, type]`` — with explicit storage type

    Example::

        class Document(BaseSurrealModel):
            embedding: VectorField[1536]
            small_vec: VectorField[384, "F64"]
    """

    def __class_getitem__(cls, params: int | tuple[int, str]) -> type:
        """``VectorField[1536]`` or ``VectorField[1536, "F64"]``."""
        if isinstance(params, tuple):
            dimension, vector_type = params
        else:
            dimension = params
            vector_type = "F32"
        return Annotated[list[float] | None, _VectorMarker(dimension, vector_type)]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _get_vector_marker(annotation: Any) -> _VectorMarker | None:
    """
    Extract the ``_VectorMarker`` instance from a type annotation.

    Works with ``Annotated[list[float] | None, _VectorMarker(...)]``.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        for arg in get_args(annotation):
            if isinstance(arg, _VectorMarker):
                return arg
    return None


def is_vector_field(field_type: Any) -> bool:
    """
    Check if a field type is a VectorField type.

    Args:
        field_type: The type annotation to check.

    Returns:
        True if the field has a ``_VectorMarker``.
    """
    return _get_vector_marker(field_type) is not None


def get_vector_info(field_type: Any) -> tuple[int, str] | None:
    """
    Extract dimension and storage type from a vector field type.

    Args:
        field_type: The type annotation to extract from.

    Returns:
        ``(dimension, vector_type)`` tuple if the field is a vector,
        ``None`` otherwise.
    """
    marker = _get_vector_marker(field_type)
    if marker is not None:
        return (marker.dimension, marker.vector_type)
    return None


__all__ = ["VectorField", "is_vector_field", "get_vector_info"]
