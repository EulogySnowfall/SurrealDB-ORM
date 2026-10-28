"""Integration tests for #328: filter keys and values() names must be field names.

Before the fix, a key whose field part was a SurrealQL expression rather than a
field name was written into the WHERE clause as given: it either failed at parse
time on the server or matched rows by a different condition. It now raises
``ValueError`` before anything is sent, and dotted paths into nested objects
keep working against the server.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, Count, Q, SurrealConfigDict, SurrealDBConnectionManager
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = "test_issue_328"


class I328Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i328_item")

    id: str | None = None
    name: str
    group: str = ""
    address: dict[str, Any] = {}


class I328Control(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i328_control")

    id: str | None = None
    label: str


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
    await I328Item.raw_query("DELETE i328_item; DELETE i328_control;")
    await I328Item(id="a", name="alpha", group="g1", address={"city": "Paris"}).save()
    await I328Item(id="b", name="beta", group="g1", address={"city": "Lyon"}).save()
    await I328Item(id="c", name="gamma", group="g2", address={"city": "Paris"}).save()
    await I328Control(id="one", label="kept").save()
    await I328Control(id="two", label="kept").save()
    yield
    await I328Item.raw_query("DELETE i328_item; DELETE i328_control;")


async def _snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    items = await I328Item.raw_query("SELECT * FROM i328_item ORDER BY id;")
    control = await I328Control.raw_query("SELECT * FROM i328_control ORDER BY id;")
    return items, control


@pytest.mark.parametrize("key", ["string::len(name)", "name x", "name;", "name\n", "address.city x__exact"])
async def test_non_field_key_raises_and_changes_nothing(key: str) -> None:
    before = await _snapshot()
    with pytest.raises(ValueError, match="Invalid filter key"):
        await I328Item.objects().filter(**{key: 5}).exec()
    with pytest.raises(ValueError, match="Invalid filter key"):
        await I328Item.objects().filter(Q(name="alpha") | Q(**{key: 5})).exec()
    assert await _snapshot() == before
    assert len(before[1]) == 2


async def test_values_with_non_field_name_raises() -> None:
    with pytest.raises(ValueError, match="values"):
        await I328Item.objects().values("group, name").annotate(n=Count()).exec()


async def test_dotted_path_filter_still_matches() -> None:
    rows = await I328Item.objects().filter(**{"address.city": "Paris"}).order_by("name").exec()
    assert [r.name for r in rows] == ["alpha", "gamma"]


async def test_valid_filters_and_grouping_still_work() -> None:
    rows = await I328Item.objects().filter(Q(name__startswith="a") | Q(group="g2")).order_by("name").exec()
    assert [r.name for r in rows] == ["alpha", "gamma"]
    stats = await I328Item.objects().values("group").annotate(n=Count()).exec()
    assert sorted((s["group"], s["n"]) for s in stats) == [("g1", 2), ("g2", 1)]
