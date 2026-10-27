"""Integration tests for #233: the atomic array helpers against a real server.

``atomic_set_add`` was documented as a set-like add but ran ``SET f += $value``,
which appends: two calls with the same value left two copies. It now runs
``array::add(f ?? [], $value)``. ``atomic_append`` starts from ``[]`` too,
because ``array::append`` rejects a missing field — harmless while statement
errors were swallowed, an exception now that they surface.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk.exceptions import QueryError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_233"


class I233Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i233_items")

    id: str | None = None
    tags: list[str] | None = None


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
async def clean_table() -> AsyncGenerator[None, Any]:
    """A fresh table with one record holding ``['x']`` and one without the field."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i233_items; DEFINE TABLE i233_items SCHEMALESS;")
    await client.query("CREATE i233_items:tagged SET tags = ['x']; CREATE i233_items:bare;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i233_items;")


async def _tags(record: str) -> list[str] | None:
    return (await I233Item.objects().get(record)).tags


@pytest.mark.integration
async def test_set_add_twice_keeps_one_copy() -> None:
    await I233Item.atomic_set_add("tagged", "tags", "p1")
    records = await I233Item.atomic_set_add("tagged", "tags", "p1")

    assert await _tags("tagged") == ["x", "p1"]
    assert [r["tags"] for r in records] == [["x", "p1"]]


@pytest.mark.integration
async def test_set_add_present_value_is_a_no_op() -> None:
    await I233Item.atomic_set_add("tagged", "tags", "x")

    assert await _tags("tagged") == ["x"]


@pytest.mark.integration
async def test_set_add_on_missing_field_creates_the_array() -> None:
    await I233Item.atomic_set_add("bare", "tags", "p1")

    assert await _tags("bare") == ["p1"]


@pytest.mark.integration
async def test_append_on_missing_field_creates_the_array() -> None:
    records = await I233Item.atomic_append("bare", "tags", "p1")

    assert await _tags("bare") == ["p1"]
    assert [r["tags"] for r in records] == [["p1"]]


@pytest.mark.integration
async def test_append_twice_keeps_both_copies() -> None:
    await I233Item.atomic_append("tagged", "tags", "p1")
    await I233Item.atomic_append("tagged", "tags", "p1")

    assert await _tags("tagged") == ["x", "p1", "p1"]


@pytest.mark.integration
async def test_remove_drops_every_occurrence() -> None:
    await I233Item.atomic_append("tagged", "tags", "x")
    await I233Item.atomic_remove("tagged", "tags", "x")

    assert await _tags("tagged") == []


@pytest.mark.integration
@pytest.mark.parametrize("helper", ["atomic_append", "atomic_set_add"])
async def test_rejected_statement_raises(helper: str) -> None:
    """A write the server rejects raises with its message; the record is unchanged."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("DEFINE FIELD OVERWRITE tags ON i233_items TYPE option<array<string>> ASSERT $value.len() < 2;")

    with pytest.raises(QueryError, match="tags"):
        await getattr(I233Item, helper)("tagged", "tags", "p1")

    assert await _tags("tagged") == ["x"]
