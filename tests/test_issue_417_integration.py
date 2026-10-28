"""
Integration tests for issue #417 — annotation aliases and geo arguments, on the pinned server.

An alias, a ``GeoDistance`` field or a ``GeoDistance`` coordinate that is not
what it claims to be is now refused before a query is built. These tests run
each rejected value through the ORM against a real server and read back a
control table: its rows must be exactly what the test wrote, and a valid
annotation must still return the expected rows.

Run with: pytest -m integration tests/test_issue_417_integration.py
"""

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from src import surreal_orm
from src.surreal_orm import GeoDistance, SearchScore
from src.surreal_orm.aggregations import Count, Sum
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.query_set import QuerySet
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_417"

ORDERS = "i417_orders"
CONTROL = "i417_control"

# Each text ends the statement the ORM builds and appends one that marks the
# control rows. Before the fix the server ran both statements.
TAIL = f"; UPDATE {CONTROL} SET touched = true; SELECT *"
GROUP_TAIL = f" FROM {ORDERS} GROUP BY status; UPDATE {CONTROL} SET touched = true; SELECT status, count() AS m"


class I417Order(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=ORDERS)

    id: str | None = None
    status: str = "open"
    amount: int = 0
    location: Any = None


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    """Point the ORM at the test database."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


async def _query(sql: str) -> list[Any]:
    """Run SQL, fail the test on any statement-level error, return each statement's result."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value == "ERR"]
    assert errors == [], f"{errors}\nSQL: {sql}"
    return [r.result for r in response.results]


@pytest.fixture(autouse=True)
async def seed() -> AsyncIterator[None]:
    """Fresh orders and control rows for each test."""

    async def cleanup() -> None:
        await _query(f"REMOVE TABLE IF EXISTS {ORDERS}; REMOVE TABLE IF EXISTS {CONTROL};")

    await cleanup()
    await _query(
        f"CREATE {CONTROL}:one SET v = 1; CREATE {CONTROL}:two SET v = 2;"
        f"CREATE {ORDERS}:a SET status = 'open', amount = 3, location = (1.0, 2.0);"
        f"CREATE {ORDERS}:b SET status = 'open', amount = 4, location = (1.0, 2.0);"
        f"CREATE {ORDERS}:c SET status = 'paid', amount = 5, location = (3.0, 4.0);"
    )
    yield
    await cleanup()


async def _control_rows() -> list[dict[str, Any]]:
    rows = (await _query(f"SELECT id, v, touched FROM {CONTROL} ORDER BY v;"))[0]
    return [{"v": row["v"], "touched": row.get("touched")} for row in rows]


UNTOUCHED = [{"v": 1, "touched": None}, {"v": 2, "touched": None}]


def _rejected_cases() -> dict[str, Callable[[], QuerySet[I417Order]]]:
    return {
        "group alias": lambda: I417Order.objects().values("status").annotate(**{"n" + GROUP_TAIL: Count()}),
        "group alias with Sum": lambda: (
            I417Order.objects()
            .values("status")
            .annotate(**{"n" + GROUP_TAIL.replace("count()", "math::sum(amount)"): Sum("amount")})
        ),
        "geo alias": lambda: I417Order.objects().annotate(**{f"d FROM {ORDERS}" + TAIL: GeoDistance("location", (1, 2))}),
        "search alias": lambda: I417Order.objects().annotate(**{f"s FROM {ORDERS}" + TAIL: SearchScore(0)}),
        "geo field": lambda: I417Order.objects().annotate(
            d=GeoDistance(f"location, (1, 2)) AS d FROM {ORDERS}{TAIL}, geo::distance(location", (1, 2))
        ),
        "geo coordinate": lambda: I417Order.objects().annotate(
            d=GeoDistance("location", (f"1, 2)) AS d FROM {ORDERS}{TAIL}, (1", 2))  # type: ignore[arg-type]
        ),
    }


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_rejected_cases()))
async def test_rejected_annotation_leaves_the_control_rows_untouched(case: str) -> None:
    raised: Exception | None = None
    try:
        await _rejected_cases()[case]().exec()
    except (ValueError, TypeError) as exc:
        raised = exc

    # Checked first: the rows are what matters, whatever the call returned.
    assert await _control_rows() == UNTOUCHED
    assert raised is not None, "the value was not refused before the query was sent"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_valid_group_annotation_still_returns_the_groups() -> None:
    rows = await I417Order.objects().values("status").annotate(n=Count(), total=Sum("amount")).exec()

    assert sorted((r["status"], r["n"], r["total"]) for r in rows) == [("open", 2, 7), ("paid", 1, 5)]
    assert await _control_rows() == UNTOUCHED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_valid_geo_distance_with_int_and_float_point() -> None:
    near = await I417Order.objects().filter(status="paid").annotate(d=GeoDistance("location", (3, 4.0))).exec()

    assert len(near) == 1
    assert await _control_rows() == UNTOUCHED
