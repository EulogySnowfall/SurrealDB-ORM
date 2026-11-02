"""Integration tests for the advanced queries of 0.38.0 against a real server.

The data (spec §1.1), written with ``raw_query()`` because o4's object
``items`` and string ``tags`` cannot go through a typed model::

    aqi_order:o1  customer {name Ann, address {city Paris, zip 75001}, orders [{total 5}, {total 200}]},
                  buyer aqi_cust:ann, items [{a, qty 1, price 10}, {b, qty 5, price 3}, {c, qty 2, price 50}],
                  tags ['x', 'xy'], orders [{open, lines [{qty 1}, {qty 3}]}, {done, lines []}]
    aqi_order:o2  customer {name Bob}, items [{a, qty 3, price 10}], tags ['z'], orders [{open, lines [{qty 1}]}]
    aqi_order:o3  items [], tags []
    aqi_order:o4  items {sku z, qty 9} (an object), tags 'x' (a string)
    aqi_order:o5  note 'no arrays'
    aqi_cust:ann  name Ann, tier gold
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, Q, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.query_set import QuerySet
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_advanced_queries"

pytestmark = pytest.mark.integration


class AQIOrder(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="aqi_order")

    id: str | None = None
    customer: dict[str, Any] | None = None
    items: Any = None
    tags: Any = None
    orders: list[dict[str, Any]] | None = None
    buyer: Any = None
    note: str | None = None


SEED = """
DELETE aqi_order; DELETE aqi_cust;
CREATE aqi_cust:ann CONTENT { name: 'Ann', tier: 'gold' };
CREATE aqi_order:o1 CONTENT {
    customer: { name: 'Ann', address: { city: 'Paris', zip: '75001' }, orders: [{ total: 5 }, { total: 200 }] },
    buyer: aqi_cust:ann,
    items: [{ sku: 'a', qty: 1, price: 10 }, { sku: 'b', qty: 5, price: 3 }, { sku: 'c', qty: 2, price: 50 }],
    tags: ['x', 'xy'],
    orders: [{ status: 'open', lines: [{ qty: 1 }, { qty: 3 }] }, { status: 'done', lines: [] }]
};
CREATE aqi_order:o2 CONTENT {
    customer: { name: 'Bob' },
    items: [{ sku: 'a', qty: 3, price: 10 }],
    tags: ['z'],
    orders: [{ status: 'open', lines: [{ qty: 1 }] }]
};
CREATE aqi_order:o3 CONTENT { items: [], tags: [] };
CREATE aqi_order:o4 CONTENT { items: { sku: 'z', qty: 9 }, tags: 'x' };
CREATE aqi_order:o5 CONTENT { note: 'no arrays' };
"""


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, Any]:
    """Point the ORM at a database of its own for this module."""
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
async def seed() -> None:
    """Write the module docstring's data before each test."""
    await AQIOrder.raw_query(SEED)


async def _ids(qs: QuerySet[AQIOrder]) -> list[str]:
    return sorted(str(order.id) for order in await qs.exec())


def _orders() -> QuerySet[AQIOrder]:
    return AQIOrder.objects()


# ---------------------------------------------------------------------------
# I1 — record sets of C1-C6 and C25
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (lambda: {"items__any": Q(qty__gt=2)}, ["o1", "o2"]),  # C1
        (lambda: {"items__none": Q(qty__gt=2)}, ["o3", "o4", "o5"]),  # C2
        (lambda: {"items__all": Q(qty__gt=1)}, ["o2", "o3"]),  # C3
        (lambda: {"orders__any": Q(status="open", lines__any=Q(qty__gt=2))}, ["o1"]),  # C4
        (lambda: {"tags__any": Q(this__startswith="x")}, ["o1"]),  # C5
        (lambda: {"tags__any": Q(this__in=["z", "q"])}, ["o2"]),  # C6
        (lambda: {"customer.orders__any": Q(total__gt=100)}, ["o1"]),  # C25
    ],
    ids=["c1-any", "c2-none", "c3-all", "c4-nested", "c5-this-startswith", "c6-this-in", "c25-dotted"],
)
async def test_record_sets(kwargs: Callable[[], dict[str, Any]], expected: list[str]) -> None:
    assert await _ids(_orders().filter(**kwargs())) == expected


async def test_count() -> None:
    assert await _orders().filter(items__any=Q(qty__gt=2)).count() == 2
    assert await _orders().filter(items__none=Q(qty__gt=2)).count() == 3
    assert await _orders().filter(items__all=Q(qty__gt=1)).count() == 2


async def test_not_q_any_equals_none() -> None:
    negated = await _ids(_orders().filter(~Q(items__any=Q(qty__gt=2))))
    assert negated == await _ids(_orders().filter(items__none=Q(qty__gt=2)))
    assert negated == ["o3", "o4", "o5"]


async def test_q_combinations() -> None:
    either = Q(items__any=Q(sku="a") | Q(sku="b")) | Q(note="no arrays")
    assert await _ids(_orders().filter(either)) == ["o1", "o2", "o5"]

    both = Q(items__any=Q(qty__gt=2)) & ~Q(tags__any=Q(this="z"))
    assert await _ids(_orders().filter(both)) == ["o1"]

    mixed = _orders().filter(Q(items__any=Q(price__gte=50)) | Q(tags__any=Q(this="z")), items__all=Q(qty__gte=1))
    assert await _ids(mixed) == ["o1", "o2"]


async def test_declared_variable_inside_element_condition() -> None:
    qs = _orders().variables(min=4).filter(items__any=Q(qty__gt="$min"))
    assert await _ids(qs) == ["o1"]


# ---------------------------------------------------------------------------
# I12 (filter half) — L2: a dotted field part crossing an array (M26)
# ---------------------------------------------------------------------------


async def test_dotted_field_part_through_an_array_is_the_documented_limitation() -> None:
    """``orders.lines`` is one array per order: ``[[], []]`` is truthy, so o1 and o2 match although no line has
    ``qty > 5``. The nested form is the correct one. If a server release changes this, this test says so."""
    assert await _ids(_orders().filter(**{"orders.lines__any": Q(qty__gt=5)})) == ["o1", "o2"]
    assert await _ids(_orders().filter(orders__any=Q(lines__any=Q(qty__gt=5)))) == []
