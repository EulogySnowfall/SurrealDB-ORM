"""Unit tests for #444 (v2): annotate() aliases and annotation arguments are checked.

Aliases, aggregation fields, geo points and search references are written into
the query text, so each one is checked before any request is built.
"""

from __future__ import annotations

from typing import Any

import pytest

from surreal_orm import (
    Avg,
    BaseSurrealModel,
    Count,
    GeoDistance,
    Max,
    Min,
    SearchHighlight,
    SearchScore,
    Subquery,
    Sum,
    SurrealConfigDict,
)


class I444Order(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i444_order")

    id: str | None = None
    status: str = ""
    amount: float = 0.0


BAD_ALIASES = ["bad alias", "a;b", "1abc", "", "x\n", "a.b", "a-b"]


# ── annotate() aliases ──────────────────────────────────────────────────────


@pytest.mark.parametrize("alias", BAD_ALIASES)
def test_annotate_rejects_non_identifier_alias(alias: str) -> None:
    qs = I444Order.objects().values("status")
    with pytest.raises(ValueError, match="annotation alias"):
        qs.annotate(**{alias: Count()})
    assert qs._annotations == {}


def test_annotate_rejects_subquery_alias() -> None:
    inner = I444Order.objects().filter(amount__gte=10).select("status")
    qs = I444Order.objects()
    with pytest.raises(ValueError, match="annotation alias"):
        qs.annotate(**{"n; x y": Subquery(inner)})
    assert qs._annotations == {}


@pytest.mark.parametrize(
    "annotation",
    [
        SearchScore(0),
        SearchHighlight("<b>", "</b>", 0),
        GeoDistance("loc", (1.0, 2.0)),
        Sum("amount"),
    ],
)
def test_annotate_rejects_bad_alias_for_every_kind(annotation: Any) -> None:
    with pytest.raises(ValueError, match="annotation alias"):
        I444Order.objects().annotate(**{"x y": annotation})


def test_annotate_accepts_identifier_aliases() -> None:
    qs = I444Order.objects().values("status").annotate(count=Count(), _total=Sum("amount"))
    assert list(qs._annotations) == ["count", "_total"]


# ── to_surql(alias) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "annotation",
    [
        Count(),
        Sum("amount"),
        Avg("amount"),
        Min("amount"),
        Max("amount"),
        SearchScore(0),
        SearchHighlight("<b>", "</b>", 0),
        GeoDistance("loc", (1.0, 2.0)),
    ],
)
@pytest.mark.parametrize("alias", BAD_ALIASES)
def test_to_surql_rejects_non_identifier_alias(annotation: Any, alias: str) -> None:
    with pytest.raises(ValueError, match="annotation alias"):
        annotation.to_surql(alias)


# ── aggregation fields ──────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", [Sum, Avg, Min, Max])
@pytest.mark.parametrize("field", ["a b", "a;b", "a.", ".a", "a..b", "a.*", "", "x\n"])
def test_aggregation_rejects_bad_field(cls: Any, field: str) -> None:
    with pytest.raises(ValueError, match="aggregation field"):
        cls(field)


@pytest.mark.parametrize(
    ("cls", "fn"),
    [(Sum, "math::sum"), (Avg, "math::mean"), (Min, "math::min"), (Max, "math::max")],
)
def test_aggregation_accepts_dotted_path(cls: Any, fn: str) -> None:
    assert cls("address.city").to_surql("total") == f"{fn}(address.city) AS total"


def test_aggregation_output_unchanged() -> None:
    assert Count().to_surql("count") == "count() AS count"
    assert Sum("amount").to_surql("total") == "math::sum(amount) AS total"
    assert Avg("amount").to_surql("avg") == "math::mean(amount) AS avg"
    assert Min("amount").to_surql("lo") == "math::min(amount) AS lo"
    assert Max("amount").to_surql("hi") == "math::max(amount) AS hi"


# ── GeoDistance ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "point",
    [("1", 2), (True, 2), (1, False), (None, 2), ("1) AS a, (1", 2), (1, 2, 3), (1,), "12", 5],
)
def test_geo_distance_rejects_non_numeric_point(point: Any) -> None:
    with pytest.raises(TypeError, match="GeoDistance point"):
        GeoDistance("loc", point)


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 10**400])
def test_geo_distance_rejects_non_finite_coordinate(bad: Any) -> None:
    with pytest.raises(ValueError, match="finite"):
        GeoDistance("loc", (bad, 2.0))
    with pytest.raises(ValueError, match="finite"):
        GeoDistance("loc", (2.0, bad))


@pytest.mark.parametrize("field", ["a b", "loc;", "", "loc)"])
def test_geo_distance_rejects_bad_field(field: str) -> None:
    with pytest.raises(ValueError, match="GeoDistance field"):
        GeoDistance(field, (1.0, 2.0))


def test_geo_distance_accepts_dotted_field_and_list_point() -> None:
    gd = GeoDistance("address.location", [1, -2.5])  # type: ignore[arg-type]
    assert gd.to_surql("d") == "geo::distance(address.location, (1, -2.5)) AS d"


def test_geo_distance_output_unchanged() -> None:
    assert GeoDistance("location", (-73.98, 40.74)).to_surql("dist") == ("geo::distance(location, (-73.98, 40.74)) AS dist")
    assert GeoDistance("location", (3, 4)).to_surql("dist") == "geo::distance(location, (3, 4)) AS dist"


def test_geo_distance_renders_subclass_as_its_number() -> None:
    class Coord(float):
        def __format__(self, spec: str) -> str:
            return "0) AS a, (1"

        def __repr__(self) -> str:
            return "0) AS a, (1"

        __str__ = __repr__

    sql = GeoDistance("loc", (Coord(1.5), 2.0)).to_surql("d")
    assert sql == "geo::distance(loc, (1.5, 2.0)) AS d"


def test_geo_distance_copies_point() -> None:
    point = [1.0, 2.0]
    gd = GeoDistance("loc", point)  # type: ignore[arg-type]
    point[0] = "1) AS a, (1"  # type: ignore[call-overload]
    assert gd.to_surql("d") == "geo::distance(loc, (1.0, 2.0)) AS d"


# ── SearchScore / SearchHighlight ref ───────────────────────────────────────


@pytest.mark.parametrize("ref", ["1", 1.0, True, False, None])
def test_search_score_rejects_non_int_ref(ref: Any) -> None:
    with pytest.raises(TypeError, match="ref"):
        SearchScore(ref)


def test_search_score_rejects_negative_ref() -> None:
    with pytest.raises(ValueError, match="ref"):
        SearchScore(-1)


@pytest.mark.parametrize("ref", ["1", 1.0, True, None])
def test_search_highlight_rejects_non_int_ref(ref: Any) -> None:
    with pytest.raises(TypeError, match="ref"):
        SearchHighlight("<b>", "</b>", ref)


def test_search_highlight_rejects_negative_ref() -> None:
    with pytest.raises(ValueError, match="ref"):
        SearchHighlight("<b>", "</b>", -1)


def test_search_score_renders_subclass_as_its_number() -> None:
    class Ref(int):
        def __format__(self, spec: str) -> str:
            return "0) AS a, (1"

        def __str__(self) -> str:
            return "0) AS a, (1"

    assert SearchScore(Ref(2)).to_surql("s") == "search::score(2) AS s"


def test_search_output_unchanged() -> None:
    assert SearchScore(0).to_surql("relevance") == "search::score(0) AS relevance"
    assert SearchHighlight("<b>", "</b>", 1).to_surql("snippet") == "search::highlight('<b>', '</b>', 1) AS snippet"


# ── require_int ─────────────────────────────────────────────────────────────


def test_require_int_lives_in_utils() -> None:
    from surreal_orm.utils import require_int

    assert require_int("n", 3, minimum=0) == 3
    with pytest.raises(TypeError):
        require_int("n", True, minimum=0)
    with pytest.raises(ValueError):
        require_int("n", -1, minimum=0)


def test_queryset_limit_still_checked() -> None:
    with pytest.raises(TypeError):
        I444Order.objects().limit("5")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        I444Order.objects().offset(-1)
