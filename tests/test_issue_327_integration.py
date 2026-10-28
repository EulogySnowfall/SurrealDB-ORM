"""Integration tests for #327: ``$``-prefixed filter values are bound unless declared.

On the pinned server, a filter value that starts with ``$`` but is not exactly a
variable declared through ``.variables()`` must behave as data: it matches only
a record whose field holds that exact text, and running the query leaves every
other table and the schema as they were.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, Q, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.subquery import Subquery
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_327"

TAUTOLOGY = "$x OR true"
MULTI = "$x; CREATE i327_witness:extra SET v = 2; SELECT * FROM i327_items WHERE true"


class I327Item(BaseSurrealModel):
    """Records filtered by name."""

    model_config = SurrealConfigDict(table_name="i327_items")

    id: str | None = None
    name: str = Field(default="")
    age: int = 0


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
async def seed() -> AsyncGenerator[None, Any]:
    """Two ordinary records, one per tricky value, and one witness row."""
    await I327Item.raw_query("REMOVE TABLE IF EXISTS i327_items; REMOVE TABLE IF EXISTS i327_witness;")
    await I327Item(id="alice", name="alice", age=30).save()
    await I327Item(id="bob", name="bob", age=40).save()
    await I327Item(id="taut", name=TAUTOLOGY, age=50).save()
    await I327Item(id="multi", name=MULTI, age=60).save()
    await I327Item.raw_query("CREATE i327_witness:one SET v = 1;")
    yield
    await I327Item.raw_query("REMOVE TABLE IF EXISTS i327_items; REMOVE TABLE IF EXISTS i327_witness;")


async def _snapshot() -> tuple[Any, Any, Any]:
    """Every row of both tables plus the database schema."""
    items = await I327Item.raw_query("SELECT * FROM i327_items ORDER BY id;")
    witness = await I327Item.raw_query("SELECT * FROM i327_witness ORDER BY id;")
    info = await I327Item.raw_query("INFO FOR DB;")
    return items, witness, info


@pytest.mark.integration
@pytest.mark.parametrize("value", [TAUTOLOGY, MULTI], ids=["operator-text", "statement-separators"])
async def test_dollar_prefixed_values_are_bound(value: str) -> None:
    """The value matches its own record only, and nothing else in the database moves."""
    before = await _snapshot()

    rows = await I327Item.objects().filter(name=value).exec()
    assert [r.name for r in rows] == [value]

    q_rows = await I327Item.objects().filter(Q(name=value) | Q(name="nobody")).exec()
    assert [r.name for r in q_rows] == [value]

    negated = await I327Item.objects().filter(~Q(name=value)).order_by("name").exec()
    assert sorted(r.name for r in negated) == sorted(n for n in ("alice", "bob", TAUTOLOGY, MULTI) if n != value)

    inner = I327Item.objects().filter(name=value).select("age")
    sub_rows = await I327Item.objects().filter(age__in=Subquery(inner)).exec()
    assert [r.name for r in sub_rows] == [value]

    assert await I327Item.objects().filter(name=value).count() == 1

    after = await _snapshot()
    assert after == before
    assert len(after[1]) == 1


@pytest.mark.integration
async def test_undeclared_reference_is_compared_as_text() -> None:
    """``$undeclared`` matches the record holding that text, not an unset variable."""
    await I327Item(id="lit", name="$undeclared").save()
    rows = await I327Item.objects().filter(name="$undeclared").exec()
    assert [r.name for r in rows] == ["$undeclared"]


@pytest.mark.integration
async def test_declared_reference_still_works() -> None:
    """The documented ``.variables()`` form is unchanged."""
    rows = await I327Item.objects().filter(age__gte="$min_age").variables(min_age=45).order_by("age").exec()
    assert [r.age for r in rows] == [50, 60]

    inner = I327Item.objects().filter(name="$wanted").variables(wanted="bob").select("age")
    sub_rows = await I327Item.objects().filter(age__in=Subquery(inner)).exec()
    assert [r.name for r in sub_rows] == ["bob"]
