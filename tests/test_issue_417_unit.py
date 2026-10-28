"""
Unit tests for issue #417 — annotation aliases and aggregation/geo arguments.

``annotate()`` writes each keyword as ``... AS <alias>``, ``Sum``/``Avg``/
``Min``/``Max``/``GeoDistance`` write their field, and ``GeoDistance`` writes
its coordinates, all into the query text. Each must be checked when it is
given, so a name that is not an identifier (or a coordinate that is not a
number) raises a clear error instead of producing a parse error or a
different query.
"""

import math
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, GeoDistance, SearchHighlight, SearchScore, Subquery, SurrealConfigDict
from src.surreal_orm.aggregations import Avg, Count, Max, Min, Sum


class U417Order(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u417_orders")

    id: str | None = None
    status: str = "open"
    amount: int = 0


def _annotations() -> dict[str, Any]:
    """One instance of every annotation class ``annotate()`` accepts."""
    return {
        "Count": Count(),
        "Sum": Sum("amount"),
        "Avg": Avg("amount"),
        "Min": Min("amount"),
        "Max": Max("amount"),
        "SearchScore": SearchScore(0),
        "SearchHighlight": SearchHighlight("<b>", "</b>", 0),
        "GeoDistance": GeoDistance("location", (-73.98, 40.74)),
        "Subquery": Subquery(U417Order.objects().select("amount")),
    }


BAD_ALIASES = ["bad alias", "x;y", "n FROM u417_orders", "", "1abc", "a.b", "a-b", "a\n", "`a`"]
ANNOTATION_NAMES = list(_annotations())


# ------------------------------------------------------------------ aliases


@pytest.mark.parametrize("kind", ANNOTATION_NAMES)
@pytest.mark.parametrize("alias", BAD_ALIASES)
def test_annotate_rejects_a_non_identifier_alias(kind: str, alias: str) -> None:
    annotation = _annotations()[kind]
    with pytest.raises(ValueError, match="annotation alias"):
        U417Order.objects().values("status").annotate(**{alias: annotation})


@pytest.mark.parametrize("kind", ANNOTATION_NAMES)
def test_annotate_rejection_leaves_the_queryset_unchanged(kind: str) -> None:
    qs = U417Order.objects().annotate(total=Count())
    with pytest.raises(ValueError):
        qs.annotate(**{"ok": _annotations()[kind], "x;y": Count()})
    assert list(qs._annotations) == ["total"]


@pytest.mark.parametrize("kind", [k for k in ANNOTATION_NAMES if k != "Subquery"])
@pytest.mark.parametrize("alias", BAD_ALIASES)
def test_to_surql_rejects_a_non_identifier_alias(kind: str, alias: str) -> None:
    with pytest.raises(ValueError, match="annotation alias"):
        _annotations()[kind].to_surql(alias)


@pytest.mark.parametrize("kind", ANNOTATION_NAMES)
def test_annotate_accepts_identifier_aliases(kind: str) -> None:
    qs = U417Order.objects().values("status").annotate(_total_2=_annotations()[kind])
    assert list(qs._annotations) == ["_total_2"]


def test_group_query_is_unchanged_for_valid_aliases() -> None:
    qs = U417Order.objects().values("status").annotate(count=Count(), total=Sum("amount"), top=Max("amount"))
    assert qs._compile_annotate_query() == (
        "SELECT status, count() AS count, math::sum(amount) AS total, math::max(amount) AS top "
        "FROM u417_orders GROUP BY status;"
    )


def test_subquery_annotation_is_unchanged_for_valid_alias() -> None:
    qs = U417Order.objects().values("status").annotate(amounts=Subquery(U417Order.objects().select("amount")))
    query = qs._compile_annotate_query()
    assert "AS amounts" in query
    assert query.startswith("LET $_sq0 = (SELECT VALUE amount FROM u417_orders);")


# ------------------------------------------------------------------ aggregation fields


@pytest.mark.parametrize("cls", [Sum, Avg, Min, Max])
@pytest.mark.parametrize("field", ["a b", "a;b", "amount) AS x FROM t; SELECT (1", "", "a..b", ".a", "a.", "*", "`a`"])
def test_aggregation_rejects_a_non_identifier_field(cls: type[Sum], field: str) -> None:
    with pytest.raises(ValueError, match="aggregation field"):
        cls(field)


@pytest.mark.parametrize(
    ("cls", "function"),
    [(Sum, "math::sum"), (Avg, "math::mean"), (Min, "math::min"), (Max, "math::max")],
)
def test_aggregation_accepts_a_dotted_path(cls: type[Sum], function: str) -> None:
    assert cls("address.city").to_surql("x") == f"{function}(address.city) AS x"


# ------------------------------------------------------------------ geo


@pytest.mark.parametrize("field", ["a b", "loc;x", "loc, (0, 0)) AS d FROM t; SELECT (1", "", "a..b"])
def test_geo_distance_rejects_a_non_identifier_field(field: str) -> None:
    with pytest.raises(ValueError, match="GeoDistance field"):
        GeoDistance(field, (1, 2))


def test_geo_distance_accepts_a_dotted_path() -> None:
    assert GeoDistance("place.location", (1, 2)).to_surql("d") == "geo::distance(place.location, (1, 2)) AS d"


@pytest.mark.parametrize(
    "point",
    [("1", 2), (1, "2"), (True, 2), (1, False), (None, 2), (1,), (1, 2, 3), "12", None, {1: 2}],
)
def test_geo_distance_rejects_a_point_that_is_not_two_numbers(point: Any) -> None:
    with pytest.raises(TypeError, match="point"):
        GeoDistance("location", point)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_geo_distance_rejects_a_non_finite_coordinate(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        GeoDistance("location", (value, 0.0))


@pytest.mark.parametrize(
    ("point", "rendered"),
    [((-73.98, 40.74), "(-73.98, 40.74)"), ((1, 2), "(1, 2)"), ((0, -0.5), "(0, -0.5)"), ([3, 4.5], "(3, 4.5)")],
)
def test_geo_distance_renders_int_and_float_as_numbers(point: Any, rendered: str) -> None:
    assert GeoDistance("location", point).to_surql("dist") == f"geo::distance(location, {rendered}) AS dist"


# ------------------------------------------------------------- search ref


def _search_annotation(cls: type[SearchScore] | type[SearchHighlight], ref: Any) -> SearchScore | SearchHighlight:
    if cls is SearchScore:
        return SearchScore(ref)
    return SearchHighlight("<b>", "</b>", ref)


@pytest.mark.parametrize("cls", [SearchScore, SearchHighlight])
@pytest.mark.parametrize("ref", ["0) AS x", "0", True, 1.5, None])
def test_search_annotation_rejects_a_ref_that_is_not_an_int(cls: Any, ref: Any) -> None:
    with pytest.raises(TypeError, match="ref"):
        _search_annotation(cls, ref)


@pytest.mark.parametrize("cls", [SearchScore, SearchHighlight])
def test_search_annotation_rejects_a_negative_ref(cls: Any) -> None:
    with pytest.raises(ValueError, match="ref"):
        _search_annotation(cls, -1)


def test_search_annotations_render_an_int_ref_unchanged() -> None:
    assert SearchScore(2).to_surql("s") == "search::score(2) AS s"
    assert SearchHighlight("<b>", "</b>", 1).to_surql("h") == "search::highlight('<b>', '</b>', 1) AS h"
