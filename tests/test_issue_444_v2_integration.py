"""
Integration tests for #444 on the v2 line, against a live SurrealDB 2.x server.

``annotate()`` aliases and the arguments of aggregation, geo and search
annotations are written into the query text. They are checked before any
request: a rejected one leaves the database as it was, and valid group and geo
annotations still return results (dotted aggregation paths included).

Run with: pytest -m integration tests/test_issue_444_v2_integration.py
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import Count, GeoDistance, Sum, SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_444_v2"


class I444Sale(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i444_sale")

    id: str | None = None
    status: str = ""
    amount: float = 0.0
    meta: dict[str, Any] | None = None


class I444Shop(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i444_shop")

    id: str | None = None
    name: str = ""
    location: Any = None
    dist: float | None = None


class I444Control(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i444_control")

    id: str | None = None
    label: str = ""


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[Any, Any]:
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def tables() -> AsyncGenerator[Any, Any]:
    client = await SurrealDBConnectionManager.get_client()
    for table in ("i444_sale", "i444_shop", "i444_control"):
        await client.query(f"REMOVE TABLE IF EXISTS {table};")
    await client.query(
        "CREATE i444_sale:1 SET status = 'paid', amount = 10.0, meta = { qty: 2 };"
        "CREATE i444_sale:2 SET status = 'paid', amount = 5.5, meta = { qty: 3 };"
        "CREATE i444_sale:3 SET status = 'open', amount = 7.0, meta = { qty: 1 };"
        "CREATE i444_shop:near SET name = 'near', location = (1.0, 2.0);"
        "CREATE i444_shop:far SET name = 'far', location = (3.0, 4.0);"
        "CREATE i444_control:keep SET label = 'keep';"
    )
    yield
    for table in ("i444_sale", "i444_shop", "i444_control"):
        await client.query(f"REMOVE TABLE IF EXISTS {table};")


async def _control_rows() -> list[dict[str, Any]]:
    client = await SurrealDBConnectionManager.get_client()
    result = await client.query("SELECT label FROM i444_control;")
    rows = result.all_records
    return sorted((dict(r) for r in rows), key=lambda r: str(r))


@pytest.mark.integration
@pytest.mark.parametrize(
    "build",
    [
        lambda: I444Sale.objects().values("status").annotate(**{"n; x y": Count()}),
        lambda: I444Sale.objects().values("status").annotate(total=Sum("amount; x y")),
        lambda: I444Shop.objects().annotate(d=GeoDistance("location", ("1; x y", 2.0))),
        lambda: I444Shop.objects().annotate(d=GeoDistance("location; x y", (1.0, 2.0))),
    ],
    ids=["alias", "aggregation-field", "geo-point", "geo-field"],
)
async def test_rejected_input_leaves_control_table_unchanged(build: Any) -> None:
    before = await _control_rows()
    assert before == [{"label": "keep"}]
    with pytest.raises((ValueError, TypeError)):
        qs = build()
        await qs.exec()
    assert await _control_rows() == before


@pytest.mark.integration
async def test_group_annotations_still_return_results() -> None:
    rows = await I444Sale.objects().values("status").annotate(count=Count(), total=Sum("amount"), qty=Sum("meta.qty")).exec()
    by_status = {r["status"]: r for r in rows}  # type: ignore[index]
    assert by_status["paid"]["count"] == 2
    assert by_status["paid"]["total"] == pytest.approx(15.5)
    assert by_status["paid"]["qty"] == 5
    assert by_status["open"]["count"] == 1
    assert by_status["open"]["qty"] == 1


@pytest.mark.integration
async def test_geo_annotation_still_returns_results() -> None:
    shops = await I444Shop.objects().annotate(dist=GeoDistance("location", (1.0, 2.0))).order_by("dist").exec()
    assert [s.name for s in shops] == ["near", "far"]
    assert shops[0].dist == pytest.approx(0.0)
    assert shops[1].dist is not None and shops[1].dist > 0
