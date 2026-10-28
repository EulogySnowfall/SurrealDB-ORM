"""Integration tests for #342 (v2 backport of #327): ``$``-prefixed filter values are bound unless declared.

On the pinned 2.x server, a filter value that starts with ``$`` but is not exactly a
variable declared through ``.variables()`` must behave as data: it matches only
a record whose field holds that exact text, and running the query leaves every
other table and the schema as they were.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, Q, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.subquery import Subquery
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_342_v2"

TAUTOLOGY = "$x OR true"
MULTI = "$x; CREATE i342_witness:extra SET v = 2; SELECT * FROM i342_items WHERE true"


class I342Item(BaseSurrealModel):
    """Records filtered by name."""

    model_config = SurrealConfigDict(table_name="i342_items")

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
    await I342Item.raw_query("REMOVE TABLE IF EXISTS i342_items; REMOVE TABLE IF EXISTS i342_witness;")
    await I342Item(id="alice", name="alice", age=30).save()
    await I342Item(id="bob", name="bob", age=40).save()
    await I342Item(id="taut", name=TAUTOLOGY, age=50).save()
    await I342Item(id="multi", name=MULTI, age=60).save()
    await I342Item.raw_query("CREATE i342_witness:one SET v = 1;")
    yield
    await I342Item.raw_query("REMOVE TABLE IF EXISTS i342_items; REMOVE TABLE IF EXISTS i342_witness;")


async def _snapshot() -> tuple[Any, Any, Any]:
    """Every row of both tables plus the database schema."""
    items = await I342Item.raw_query("SELECT * FROM i342_items ORDER BY id;")
    witness = await I342Item.raw_query("SELECT * FROM i342_witness ORDER BY id;")
    info = await I342Item.raw_query("INFO FOR DB;")
    return items, witness, info


@pytest.mark.integration
@pytest.mark.parametrize("value", [TAUTOLOGY, MULTI], ids=["operator-text", "statement-separators"])
async def test_dollar_prefixed_values_are_bound(value: str) -> None:
    """The value matches its own record only, and nothing else in the database moves."""
    before = await _snapshot()

    rows = await I342Item.objects().filter(name=value).exec()
    assert [r.name for r in rows] == [value]

    q_rows = await I342Item.objects().filter(Q(name=value) | Q(name="nobody")).exec()
    assert [r.name for r in q_rows] == [value]

    negated = await I342Item.objects().filter(~Q(name=value)).order_by("name").exec()
    assert sorted(r.name for r in negated) == sorted(n for n in ("alice", "bob", TAUTOLOGY, MULTI) if n != value)

    inner = I342Item.objects().filter(name=value).select("age")
    sub_rows = await I342Item.objects().filter(age__in=Subquery(inner)).exec()
    assert [r.name for r in sub_rows] == [value]

    assert await I342Item.objects().filter(name=value).count() == 1

    after = await _snapshot()
    assert after == before
    assert len(after[1]) == 1


@pytest.mark.integration
async def test_undeclared_reference_is_compared_as_text() -> None:
    """``$undeclared`` matches the record holding that text, not an unset variable."""
    await I342Item(id="lit", name="$undeclared").save()
    rows = await I342Item.objects().filter(name="$undeclared").exec()
    assert [r.name for r in rows] == ["$undeclared"]


@pytest.mark.integration
async def test_declared_reference_still_works() -> None:
    """The documented ``.variables()`` form is unchanged."""
    rows = await I342Item.objects().filter(age__gte="$min_age").variables(min_age=45).order_by("age").exec()
    assert [r.age for r in rows] == [50, 60]

    inner = I342Item.objects().filter(name="$wanted").variables(wanted="bob").select("age")
    sub_rows = await I342Item.objects().filter(age__in=Subquery(inner)).exec()
    assert [r.name for r in sub_rows] == ["bob"]


@pytest.mark.integration
async def test_reused_queryset_binds_value_naming_an_orm_binding() -> None:
    """``count()`` then ``exec()`` on one QuerySet agree, and ``$_f0`` stays data.

    Compiling stores the ORM's own ``$_fN`` bindings on the QuerySet; a value
    equal to one of them must not turn into a reference on the next compile.
    """
    # $_f0 holds "bob" once compiled: read back as a reference, the second
    # filter would match the record named "bob".
    qs = I342Item.objects().filter(name__startswith="bob", name="$_f0")
    assert await qs.count() == 0
    assert await qs.exec() == []
    assert await qs.count() == 0

    inner = I342Item.objects().filter(name__startswith="bob", name="$_f0").select("age")
    assert await inner.exec() == []
    assert await I342Item.objects().filter(age__in=Subquery(inner)).exec() == []


@pytest.mark.integration
@pytest.mark.parametrize("value", [TAUTOLOGY, "$undeclared"], ids=["operator-text", "undeclared"])
async def test_live_filter_matches_the_literal_only(value: str) -> None:
    """``live()`` on this line writes filter values into the LIVE SELECT text.

    A decoy record is written before the matching one; notifications on one
    connection arrive in order, so a filter that matched the decoy would
    deliver it first.
    """
    async with I342Item.objects().filter(name=value).live() as stream:
        await I342Item.raw_query("CREATE i342_items:decoy SET name = 'decoy', age = 1;")
        await I342Item.raw_query("CREATE i342_items:hit SET name = $v, age = 2;", variables={"v": value})
        event = await asyncio.wait_for(anext(stream), timeout=5.0)
    assert event.instance is not None
    assert event.instance.name == value
