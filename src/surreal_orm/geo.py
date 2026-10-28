"""
Geospatial annotation helpers for SurrealDB.

Provides ``GeoDistance`` that can be used with ``QuerySet.annotate()``
to compute distances between a field and a reference point.

Example::

    from surreal_orm import GeoDistance

    results = await Restaurant.objects().nearby(
        "location", (-73.98, 40.74), max_distance=5000,
    ).annotate(
        dist=GeoDistance("location", (-73.98, 40.74)),
    ).exec()
"""

from __future__ import annotations

import math
from typing import Any

from .utils import validate_field_path, validate_identifier


class GeoDistance:
    """
    Geo distance annotation.

    Wraps ``geo::distance(field, point)`` for use with ``annotate()``.

    Args:
        field: Name of the geometry field.
        point: Reference point as ``(longitude, latitude)`` tuple (GeoJSON order).

    Raises:
        ValueError: If ``field`` is not a field name or dotted path, or a
            coordinate is not finite.
        TypeError: If ``point`` is not two ``int``/``float`` values (``bool``
            and numeric strings are refused).

    Example::

        results = await Restaurant.objects().annotate(
            dist=GeoDistance("location", (-73.98, 40.74)),
        ).exec()
    """

    def __init__(self, field: str, point: tuple[float, float]) -> None:
        # Both are written into the query text by to_surql(), not bound.
        validate_field_path(field, "GeoDistance field")
        if (
            not isinstance(point, (tuple, list))
            or len(point) != 2
            or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in point)
        ):
            raise TypeError(f"GeoDistance point must be a (longitude, latitude) pair of int or float, got {point!r}")
        if not all(math.isfinite(c) for c in point):
            raise ValueError(f"GeoDistance point coordinates must be finite numbers, got {point!r}")
        self.field = field
        self.point = point

    def to_surql(self, alias: str) -> str:
        """Render as ``geo::distance(field, <point>) AS alias``.

        Raises:
            ValueError: If ``alias`` is not an identifier.
        """
        validate_identifier(alias, "annotation alias")
        return f"geo::distance({self.field}, ({self.point[0]}, {self.point[1]})) AS {alias}"

    def __repr__(self) -> str:
        return f"GeoDistance({self.field!r}, {self.point!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, GeoDistance):
            return self.field == other.field and self.point == other.point
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("GeoDistance", self.field, self.point))


__all__ = ["GeoDistance"]
