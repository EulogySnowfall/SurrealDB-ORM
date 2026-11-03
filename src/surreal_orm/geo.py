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

_POINT_TYPE_MESSAGE = "GeoDistance point must be a (longitude, latitude) pair of int or float, got {point!r}"


def _plain_coordinate(value: Any, point: Any) -> int | float:
    """Return ``value`` as a plain, finite ``int`` or ``float``.

    The coordinates are written into the query text, so only numbers are
    accepted.

    Raises:
        TypeError: If ``value`` is not an ``int``/``float`` (``bool`` and
            numeric strings refused).
        ValueError: If ``value`` is not finite, or is an ``int`` too large to
            be read as a float.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(_POINT_TYPE_MESSAGE.format(point=point))
    # The base-class conversions, so a subclass cannot substitute another
    # value through an override.
    plain: int | float = int.__index__(value) if isinstance(value, int) else float.__float__(value)
    try:
        finite = math.isfinite(plain)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"GeoDistance point coordinates must be finite numbers, got {point!r}")
    return plain


def _render_coordinate(value: int | float) -> str:
    """Render a plain coordinate with the base-class ``__repr__``.

    For a plain ``int``/``float`` this is the text ``str()`` gave before.
    """
    return int.__repr__(value) if isinstance(value, int) else float.__repr__(value)


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
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            raise TypeError(_POINT_TYPE_MESSAGE.format(point=point))
        self.field = field
        # An immutable copy of plain numbers: to_surql() renders exactly what
        # was checked here, whatever the caller's object does afterwards.
        self.point: tuple[int | float, int | float] = (
            _plain_coordinate(point[0], point),
            _plain_coordinate(point[1], point),
        )

    def to_surql(self, alias: str) -> str:
        """Render as ``geo::distance(field, <point>) AS alias``.

        Raises:
            ValueError: If ``alias`` is not an identifier.
        """
        validate_identifier(alias, "annotation alias")
        lon, lat = (_render_coordinate(c) for c in self.point)
        return f"geo::distance({self.field}, ({lon}, {lat})) AS {alias}"

    def __repr__(self) -> str:
        return f"GeoDistance({self.field!r}, {self.point!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, GeoDistance):
            return self.field == other.field and self.point == other.point
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("GeoDistance", self.field, self.point))


__all__ = ["GeoDistance"]
