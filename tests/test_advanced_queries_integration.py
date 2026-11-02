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

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest

from src.surreal_orm import (
    BaseSurrealModel,
    FieldPath,
    ModelChangeEvent,
    Q,
    QueryCache,
    SurrealConfigDict,
    SurrealDBConnectionManager,
)
from src.surreal_orm.query_set import QuerySet
from surreal_sdk.exceptions import QueryError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_advanced_queries"

# Set from the server version (SDK ``version()``) by the module fixture: a
# ``FieldPath.where()`` reached on a value that is not an array (o4) gives
# ``None`` on 3.3.x and a filtered one-element array on 3.2.x (spec D6).
ON_32 = False

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
    client = await SurrealDBConnectionManager.get_client()
    version = await client.version()
    global ON_32
    ON_32 = "3.2." in version
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


# ---------------------------------------------------------------------------
# I2, I10, I12 (projection half) — FieldPath annotation values per record
# ---------------------------------------------------------------------------

IDS = ["o1", "o2", "o3", "o4", "o5"]


def _d6(on_33: Any, on_32: Any) -> Any:
    """The o4 cell of a ``where()`` reached on a non-array value (D6)."""
    return on_32 if ON_32 else on_33


async def _values(*aliases: str, **paths: FieldPath) -> dict[str, tuple[Any, ...] | Any]:
    """Run ``annotate(**paths)`` and return ``{id: value}`` (one alias) or ``{id: (values...)}``."""
    rows = await _orders().annotate(**paths).exec()
    assert all(isinstance(row, AQIOrder) for row in rows)
    names = aliases or tuple(paths)
    out: dict[str, Any] = {}
    for row in rows:
        values = tuple(getattr(row, name) for name in names)
        out[str(row.id)] = values[0] if len(values) == 1 else values
    assert sorted(out) == IDS
    return out


async def test_c7_where_then_pick() -> None:
    got = await _values(big=FieldPath("items").where(qty__gt=1).pick("sku", total="price"))
    assert got == {
        "o1": [{"sku": "b", "total": 3}, {"sku": "c", "total": 50}],
        "o2": [{"sku": "a", "total": 10}],
        "o3": [],
        "o4": _d6(None, [{"sku": "z", "total": None}]),
        "o5": None,
    }


async def test_c8_pick_with_dotted_source() -> None:
    got = await _values(who=FieldPath("customer").pick("name", city="address.city"))
    assert got == {
        "o1": {"name": "Ann", "city": "Paris"},
        "o2": {"name": "Bob", "city": None},
        "o3": None,
        "o4": None,
        "o5": None,
    }


async def test_c9_indexes() -> None:
    got = await _values(
        qty_a=FieldPath("items").where(sku="a").first().field("qty"),
        last_sku=FieldPath("items").last().field("sku"),
        second=FieldPath("items").at(1).field("sku"),
    )
    assert got == {
        "o1": (1, "c", "b"),
        "o2": (3, "a", None),
        "o3": (None, None, None),
        "o4": (None, "z", None),
        "o5": (None, None, None),
    }


async def test_c12_or_condition() -> None:
    got = await _values(skus=FieldPath("items").where(Q(qty__gt=2) | Q(sku="a")).field("sku"))
    assert got == {"o1": ["a", "b"], "o2": ["a"], "o3": [], "o4": _d6(None, ["z"]), "o5": None}


async def test_c13_record_link() -> None:
    got = await _values(b=FieldPath("buyer").pick("name", "tier"))
    assert got == {"o1": {"name": "Ann", "tier": "gold"}, "o2": None, "o3": None, "o4": None, "o5": None}


async def test_c14_declared_variable() -> None:
    rows = await _orders().variables(min=2).annotate(skus=FieldPath("items").where(qty__gt="$min").field("sku")).exec()
    got = {str(row.id): row.skus for row in rows}  # type: ignore[attr-defined]
    assert got == {"o1": ["b"], "o2": ["a"], "o3": [], "o4": _d6(None, ["z"]), "o5": None}


async def test_c15_nested_element_lookup() -> None:
    got = await _values(st=FieldPath("orders").where(lines__any=Q(qty__gt=2)).field("status"))
    assert got == {"o1": ["open"], "o2": [], "o3": None, "o4": None, "o5": None}


async def test_c16_where_inside_pick_value() -> None:
    got = await _values(o=FieldPath("orders").where(status="open").pick("status", lines=FieldPath("lines").where(qty__gt=1)))
    assert got == {
        "o1": [{"status": "open", "lines": [{"qty": 3}]}],
        "o2": [{"status": "open", "lines": []}],
        "o3": None,
        "o4": None,
        "o5": None,
    }


async def test_c24_rule_p_inside_pick_value() -> None:
    got = await _values(
        o=FieldPath("orders")
        .where(status="open")
        .pick("status", first=FieldPath("lines").field("qty").first(), l=FieldPath("lines").field("qty").last())
    )
    assert got == {
        "o1": [{"status": "open", "first": 1, "l": 3}],
        "o2": [{"status": "open", "first": 1, "l": 1}],
        "o3": None,
        "o4": None,
        "o5": None,
    }


# I10 — composition matrix (rule P): every step applies to the value built so far.
COMPOSITION = [
    # field("sku") x {first, last, at(1), where} — C19, C21
    ("field-first", lambda: FieldPath("items").field("sku").first(), {"o1": "a", "o2": "a"}, {}),
    ("field-last", lambda: FieldPath("items").field("sku").last(), {"o1": "c", "o2": "a", "o4": "z"}, {}),
    ("field-at1", lambda: FieldPath("items").field("sku").at(1), {"o1": "b"}, {}),
    (
        "field-where",
        lambda: FieldPath("items").field("sku").where(this="b"),
        {"o1": ["b"], "o2": [], "o3": []},
        {"o4": (None, [])},
    ),
    # pick("sku") x {first, last, at(1), where} — C20, C21
    ("pick-first", lambda: FieldPath("items").pick("sku").first(), {"o1": {"sku": "a"}, "o2": {"sku": "a"}}, {}),
    (
        "pick-last",
        lambda: FieldPath("items").pick("sku").last(),
        {"o1": {"sku": "c"}, "o2": {"sku": "a"}, "o4": {"sku": "z"}},
        {},
    ),
    ("pick-at1", lambda: FieldPath("items").pick("sku").at(1), {"o1": {"sku": "b"}}, {}),
    (
        "pick-where",
        lambda: FieldPath("items").pick("sku").where(sku="b"),
        {"o1": [{"sku": "b"}], "o2": [], "o3": []},
        {"o4": (None, [])},
    ),
    # C22 — a dotted root through objects
    ("dotted-where", lambda: FieldPath("customer.orders").where(total__gt=100), {"o1": [{"total": 200}]}, {}),
    # C23 — where() then field() then an index
    (
        "where-field-first",
        lambda: FieldPath("items").where(qty__gt=1).field("sku").first(),
        {"o1": "b", "o2": "a"},
        {"o4": (None, "z")},
    ),
    (
        "where-field-last",
        lambda: FieldPath("items").where(qty__gt=1).field("sku").last(),
        {"o1": "c", "o2": "a"},
        {"o4": (None, "z")},
    ),
]


@pytest.mark.parametrize(("path", "expected", "d6"), [c[1:] for c in COMPOSITION], ids=[c[0] for c in COMPOSITION])
async def test_composition_matrix(
    path: Callable[[], FieldPath], expected: dict[str, Any], d6: dict[str, tuple[Any, Any]]
) -> None:
    """Unlisted records are ``None``; ``d6`` holds the (3.3.x, 3.2.x) values of a ``where()`` on o4."""
    want = {rid: expected.get(rid) for rid in IDS}
    for rid, (on_33, on_32) in d6.items():
        want[rid] = _d6(on_33, on_32)
    assert await _values(v=path()) == want


async def test_where_over_a_field_read_through_an_array_is_the_documented_limitation() -> None:
    """L2 (M25): ``orders.lines`` is one array per order, and a ``where()`` on that list tests each whole array, so
    the lists come back unfiltered. ``pick(lines=FieldPath("lines").where(...))`` (C16) is the correct form."""
    got = await _values(lines=FieldPath("orders").field("lines").where(qty__gt=1))
    assert got == {"o1": [[{"qty": 1}, {"qty": 3}], []], "o2": [[{"qty": 1}]], "o3": None, "o4": None, "o5": None}


# ---------------------------------------------------------------------------
# I3 — C10: ordering by an annotation alias, with a filter and a limit
# ---------------------------------------------------------------------------


async def test_c10_order_by_alias_and_limit() -> None:
    rows = (
        await _orders()
        .annotate(n=FieldPath("items").where(qty__gt=1).first().field("qty"))
        .filter(items__any=Q(qty__gt=2))
        .order_by("-n")
        .limit(2)
        .exec()
    )
    assert [(str(row.id), row.n) for row in rows] == [("o1", 5), ("o2", 3)]  # type: ignore[attr-defined]

    ascending = (
        await _orders()
        .annotate(n=FieldPath("items").where(qty__gt=1).first().field("qty"))
        .filter(items__any=Q(qty__gt=2))
        .order_by("n")
        .exec()
    )
    assert [(str(row.id), row.n) for row in ascending] == [("o2", 3), ("o1", 5)]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# I9 — get(id) and first() compute the annotation
# ---------------------------------------------------------------------------


async def test_get_with_a_path_annotation() -> None:
    qs = _orders().annotate(city=FieldPath("customer.address.city"), s0=FieldPath("items").field("sku").first())
    order = await qs.get("o1")
    assert str(order.id) == "o1"
    assert (order.city, order.s0) == ("Paris", "a")  # type: ignore[attr-defined]

    partial = await qs.get("aqi_order:o5")
    assert (partial.city, partial.s0) == (None, None)  # type: ignore[attr-defined]

    with pytest.raises(AQIOrder.DoesNotExist):
        await qs.get("missing")


async def test_first_with_a_path_annotation() -> None:
    order = await _orders().filter(note="no arrays").annotate(city=FieldPath("customer.address.city")).first()
    assert str(order.id) == "o5"
    assert order.city is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# I4 — C17: bulk writes on an element filter
# ---------------------------------------------------------------------------


async def _flagged() -> list[str]:
    return await _ids(_orders().filter(flagged=True))


async def test_c17_bulk_delete() -> None:
    assert await _orders().filter(items__any=Q(qty__gt=2)).bulk_delete() == 2
    assert await _ids(_orders()) == ["o3", "o4", "o5"]


async def test_c17b_bulk_update() -> None:
    assert await _orders().filter(items__all=Q(qty__gt=1)).bulk_update({"flagged": True}) == 2
    assert await _flagged() == ["o2", "o3"]


async def test_bulk_writes_atomic() -> None:
    assert await _orders().filter(items__all=Q(qty__gt=1)).bulk_update({"flagged": True}, atomic=True) == 2
    assert await _flagged() == ["o2", "o3"]
    assert await _orders().filter(items__any=Q(qty__gt=2)).bulk_delete(atomic=True) == 2
    assert await _ids(_orders()) == ["o3", "o4", "o5"]


# ---------------------------------------------------------------------------
# I5 — a statement error is raised, not read as no records (M11)
# ---------------------------------------------------------------------------


async def test_statement_error_is_raised() -> None:
    await AQIOrder.raw_query("CREATE aqi_order:o6 CONTENT { items: [{ qty: 1 }] };")
    with pytest.raises(QueryError, match="NONE"):
        await _orders().filter(items__any=Q(sku__startswith="a")).exec()
    with pytest.raises(QueryError, match="NONE"):
        await _orders().filter(items__any=Q(sku__startswith="a")).count()
    with pytest.raises(QueryError, match="NONE"):
        await _orders().filter(items__any=Q(sku__startswith="a")).bulk_delete()
    assert len(await _ids(_orders())) == 6  # nothing deleted


async def test_statement_error_from_a_path_annotation() -> None:
    await AQIOrder.raw_query("CREATE aqi_order:o6 CONTENT { items: [{ qty: 1 }] };")
    with pytest.raises(QueryError, match="NONE"):
        await _orders().annotate(a=FieldPath("items").where(sku__startswith="a")).exec()


# ---------------------------------------------------------------------------
# I6, I11 — live() with an element filter (C18, C26)
# ---------------------------------------------------------------------------


async def _write(sql: str) -> None:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    assert response.is_ok, response.raw


async def _live_events(qs: QuerySet[AQIOrder], decoys: list[str], match: str) -> list[str]:
    """
    Open ``qs.live()``, write the decoys, the match, the decoys again, then a sentinel copy of the match.

    Notifications on one connection arrive in order, so the first two events
    are ``l_match`` then ``l_sentinel`` only if no decoy was notified: the
    filter fired exactly once for the writes before the sentinel.
    """
    events: list[str] = []
    async with qs.live() as stream:
        for i, decoy in enumerate(decoys):
            await _write(f"CREATE aqi_order:l_decoy{i} CONTENT {decoy};")
        await _write(f"CREATE aqi_order:l_match CONTENT {match};")
        for i, decoy in enumerate(decoys):
            await _write(f"CREATE aqi_order:l_after{i} CONTENT {decoy};")
        await _write(f"CREATE aqi_order:l_sentinel CONTENT {match};")
        for _ in range(2):
            event: ModelChangeEvent[AQIOrder] = await asyncio.wait_for(anext(stream), timeout=5.0)
            events.append(f"{event.action.value} {event.record_id}")
    return events


async def test_c18_live() -> None:
    qs = _orders().filter(items__any=Q(qty__gt=2, sku__in=["a", "b"]))
    decoys = [
        "{ items: [{ sku: 'a', qty: 1 }, { sku: 'z', qty: 9 }] }",  # no element matches both conditions
        "{ items: 7 }",  # a scalar
        "{ items: { sku: 'a', qty: 9 } }",  # an object
    ]
    events = await _live_events(qs, decoys, "{ items: [{ sku: 'b', qty: 5 }] }")
    assert events == ["CREATE aqi_order:l_match", "CREATE aqi_order:l_sentinel"]


async def test_c26_live_this() -> None:
    qs = _orders().filter(tags__any=Q(this__startswith="x"))
    decoys = [
        "{ tags: ['a', 'b'] }",  # no element matches
        "{ tags: 'xyz' }",  # a string
        "{ note: 'no tags' }",  # missing
    ]
    events = await _live_events(qs, decoys, "{ tags: ['a', 'xa'] }")
    assert events == ["CREATE aqi_order:l_match", "CREATE aqi_order:l_sentinel"]


# ---------------------------------------------------------------------------
# I7 — composition with reachable_from() (M21)
# ---------------------------------------------------------------------------


async def test_composition_with_a_traversal() -> None:
    await AQIOrder.raw_query(
        "DELETE aqi_next; "
        "RELATE aqi_order:o1->aqi_next->aqi_order:o2; "
        "RELATE aqi_order:o2->aqi_next->aqi_order:o3; "
        "RELATE aqi_order:o1->aqi_next->aqi_order:o4;"
    )
    rows = (
        await _orders()
        .reachable_from("aqi_order:o1", "aqi_next", max_depth=3, node_filter=Q(items__all=Q(qty__gt=0)))
        .filter(items__any=Q(qty__gt=2))
        .annotate(big=FieldPath("items").where(qty__gt=1).field("sku"))
        .exec()
    )
    assert [(str(row.id), row.big) for row in rows] == [("o2", ["a"])]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# I8 — cache() on an element-filter query
# ---------------------------------------------------------------------------


async def test_cache_hit_then_invalidated_by_save() -> None:
    QueryCache.clear()
    try:
        qs = _orders().filter(items__any=Q(qty__gt=2)).cache(ttl=60)
        assert await _ids(qs) == ["o1", "o2"]
        # Written behind the ORM's back: no invalidation, the cached result is served.
        await _write("UPDATE aqi_order:o3 SET items = [{ sku: 'q', qty: 9 }];")
        assert await _ids(_orders().filter(items__any=Q(qty__gt=2)).cache(ttl=60)) == ["o1", "o2"]
        # An ORM write invalidates the table's entries.
        order = await _orders().get("o5")
        order.note = "changed"
        await order.save()
        assert await _ids(_orders().filter(items__any=Q(qty__gt=2)).cache(ttl=60)) == ["o1", "o2", "o3"]
    finally:
        QueryCache.clear()
