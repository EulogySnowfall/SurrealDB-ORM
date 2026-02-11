"""
Geospatial field types for SurrealDB ORM.

Provides ``GeoField`` and convenience aliases (``PointField``,
``PolygonField``, etc.) that map to SurrealDB's ``geometry<X>`` types.

GeoJSON format is used for storage::

    {"type": "Point", "coordinates": [-73.98, 40.74]}

Usage::

    from surreal_orm.fields import GeoField, PointField

    class Restaurant(BaseSurrealModel):
        name: str
        location: PointField
        area: GeoField["polygon"]
"""

from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema


class _GeoMarker:
    """
    Pydantic-compatible marker for geometry fields.

    Stored inside ``Annotated[dict[str, Any] | None, _GeoMarker(geo_type)]``
    to carry the SurrealDB geometry sub-type.
    """

    geo_type: str

    def __init__(self, geo_type: str = "point") -> None:
        self.geo_type = geo_type.lower()

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build a nullable dict schema with a default of None."""
        inner_schema = handler.generate_schema(dict[str, Any])
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


class GeoField:
    """
    Geospatial field for SurrealDB geometry types.

    ``GeoField["point"]`` resolves to
    ``Annotated[dict[str, Any] | None, _GeoMarker("point")]``.

    Example::

        class Restaurant(BaseSurrealModel):
            location: GeoField["point"]
            service_area: GeoField["polygon"]
    """

    def __class_getitem__(cls, geo_type: str) -> type:
        """``GeoField["point"]`` or ``GeoField["polygon"]``."""
        return Annotated[dict[str, Any] | None, _GeoMarker(geo_type)]  # type: ignore[return-value]


# Convenience aliases
PointField = GeoField["point"]  # type: ignore[type-arg, name-defined]
PolygonField = GeoField["polygon"]  # type: ignore[type-arg, name-defined]
LineStringField = GeoField["linestring"]  # type: ignore[type-arg, name-defined]
MultiPointField = GeoField["multipoint"]  # type: ignore[type-arg, name-defined]


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _get_geo_marker(annotation: Any) -> _GeoMarker | None:
    """Extract the ``_GeoMarker`` from a type annotation."""
    origin = get_origin(annotation)
    if origin is Annotated:
        for arg in get_args(annotation):
            if isinstance(arg, _GeoMarker):
                return arg
    return None


def is_geo_field(field_type: Any) -> bool:
    """Check if a field type is a GeoField type."""
    return _get_geo_marker(field_type) is not None


def get_geo_info(field_type: Any) -> str | None:
    """
    Extract the geometry sub-type from a geo field type.

    Returns:
        Geo type string (e.g., ``"point"``, ``"polygon"``) or None.
    """
    marker = _get_geo_marker(field_type)
    if marker is not None:
        return marker.geo_type
    return None


__all__ = [
    "GeoField",
    "PointField",
    "PolygonField",
    "LineStringField",
    "MultiPointField",
    "is_geo_field",
    "get_geo_info",
]
