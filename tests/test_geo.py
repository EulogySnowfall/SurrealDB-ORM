"""
Tests for geospatial fields and queries — GeoField, GeoDistance, nearby().
"""

from __future__ import annotations

from surreal_orm.fields.geometry import (
    GeoField,
    LineStringField,
    MultiPointField,
    PointField,
    PolygonField,
    get_geo_info,
    is_geo_field,
)
from surreal_orm.geo import GeoDistance


# ---------------------------------------------------------------------------
# GeoField types
# ---------------------------------------------------------------------------


class TestGeoField:
    """Tests for GeoField type annotations and markers."""

    def test_geo_field_point(self) -> None:
        assert is_geo_field(GeoField["point"])
        assert get_geo_info(GeoField["point"]) == "point"

    def test_geo_field_polygon(self) -> None:
        assert is_geo_field(GeoField["polygon"])
        assert get_geo_info(GeoField["polygon"]) == "polygon"

    def test_geo_field_linestring(self) -> None:
        assert is_geo_field(GeoField["linestring"])
        assert get_geo_info(GeoField["linestring"]) == "linestring"

    def test_geo_field_multipoint(self) -> None:
        assert is_geo_field(GeoField["multipoint"])
        assert get_geo_info(GeoField["multipoint"]) == "multipoint"

    def test_non_geo_field(self) -> None:
        assert not is_geo_field(str)
        assert not is_geo_field(int)
        assert not is_geo_field(dict)

    def test_get_geo_info_non_geo(self) -> None:
        assert get_geo_info(str) is None
        assert get_geo_info(int) is None

    def test_case_insensitive(self) -> None:
        """Geo types should be stored lowercase."""
        assert get_geo_info(GeoField["Point"]) == "point"
        assert get_geo_info(GeoField["POLYGON"]) == "polygon"


class TestConvenienceAliases:
    """Tests for PointField, PolygonField, etc. aliases."""

    def test_point_field(self) -> None:
        assert is_geo_field(PointField)
        assert get_geo_info(PointField) == "point"

    def test_polygon_field(self) -> None:
        assert is_geo_field(PolygonField)
        assert get_geo_info(PolygonField) == "polygon"

    def test_linestring_field(self) -> None:
        assert is_geo_field(LineStringField)
        assert get_geo_info(LineStringField) == "linestring"

    def test_multipoint_field(self) -> None:
        assert is_geo_field(MultiPointField)
        assert get_geo_info(MultiPointField) == "multipoint"


# ---------------------------------------------------------------------------
# GeoDistance annotation
# ---------------------------------------------------------------------------


class TestGeoDistance:
    """Tests for GeoDistance annotation helper."""

    def test_to_surql(self) -> None:
        gd = GeoDistance("location", (40.74, -73.98))
        result = gd.to_surql("dist")
        assert result == "geo::distance(location, (40.74, -73.98)) AS dist"

    def test_to_surql_custom_alias(self) -> None:
        gd = GeoDistance("coords", (51.5, -0.12))
        result = gd.to_surql("distance_km")
        assert result == "geo::distance(coords, (51.5, -0.12)) AS distance_km"

    def test_repr(self) -> None:
        gd = GeoDistance("location", (40.74, -73.98))
        assert repr(gd) == "GeoDistance('location', (40.74, -73.98))"

    def test_equality(self) -> None:
        a = GeoDistance("loc", (1.0, 2.0))
        b = GeoDistance("loc", (1.0, 2.0))
        assert a == b

    def test_inequality_field(self) -> None:
        a = GeoDistance("loc1", (1.0, 2.0))
        b = GeoDistance("loc2", (1.0, 2.0))
        assert a != b

    def test_inequality_point(self) -> None:
        a = GeoDistance("loc", (1.0, 2.0))
        b = GeoDistance("loc", (3.0, 4.0))
        assert a != b

    def test_hash(self) -> None:
        a = GeoDistance("loc", (1.0, 2.0))
        b = GeoDistance("loc", (1.0, 2.0))
        assert hash(a) == hash(b)

    def test_not_equal_to_other(self) -> None:
        gd = GeoDistance("loc", (1.0, 2.0))
        assert gd != "not a GeoDistance"


# ---------------------------------------------------------------------------
# GeoField in Pydantic model
# ---------------------------------------------------------------------------


class TestGeoFieldInModel:
    """Tests for using GeoField types in a Pydantic/ORM model."""

    def test_model_with_point_field(self) -> None:
        from pydantic import BaseModel

        class Store(BaseModel):
            name: str
            location: PointField = None  # type: ignore[assignment]

        store = Store(name="Test Store")
        assert store.location is None

        store2 = Store(
            name="Test Store",
            location={"type": "Point", "coordinates": [-73.98, 40.74]},
        )
        assert store2.location is not None
        assert store2.location["type"] == "Point"

    def test_model_with_polygon_field(self) -> None:
        from pydantic import BaseModel

        class Zone(BaseModel):
            area: PolygonField = None  # type: ignore[assignment]

        zone = Zone()
        assert zone.area is None

        zone2 = Zone(
            area={
                "type": "Polygon",
                "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]],
            }
        )
        assert zone2.area is not None
        assert zone2.area["type"] == "Polygon"


# ---------------------------------------------------------------------------
# Model generator — geometry type mapping
# ---------------------------------------------------------------------------


class TestModelGeneratorGeoTypes:
    """Tests for ModelCodeGenerator's geometry type handling."""

    def test_geometry_point_to_python(self) -> None:
        from surreal_orm.migrations.model_generator import ModelCodeGenerator

        gen = ModelCodeGenerator()
        result = gen._surreal_type_to_python("geometry<point>")
        assert result == 'GeoField["point"]'

    def test_geometry_polygon_to_python(self) -> None:
        from surreal_orm.migrations.model_generator import ModelCodeGenerator

        gen = ModelCodeGenerator()
        result = gen._surreal_type_to_python("geometry<polygon>")
        assert result == 'GeoField["polygon"]'

    def test_geometry_linestring_to_python(self) -> None:
        from surreal_orm.migrations.model_generator import ModelCodeGenerator

        gen = ModelCodeGenerator()
        result = gen._surreal_type_to_python("geometry<linestring>")
        assert result == 'GeoField["linestring"]'

    def test_plain_geometry_to_dict(self) -> None:
        from surreal_orm.migrations.model_generator import ModelCodeGenerator

        gen = ModelCodeGenerator()
        result = gen._surreal_type_to_python("geometry")
        assert result == "dict[str, Any]"
