"""Integration tests for #233: the atomic array helpers against a real server.

``atomic_set_add`` was documented as a set-like add but ran ``SET f += $value``,
which appends: two calls with the same value left two copies. It now runs
``array::add()`` with ``[]`` for a missing field. ``atomic_append`` starts from
``[]`` too, because ``array::append`` rejects a missing field — harmless while
statement errors were swallowed, an exception now that they surface.

``array::add()`` rejects a ``set``, the type migrations generate for a Python
``set[T]`` (``set<T>``), and ``+=`` already deduplicated there; so a set goes
through ``<array>`` and back through ``<set>``, keeping its type.
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


class I233SetItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i233_sets")

    id: str | None = None
    processed_by: list[str] | None = None


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


@pytest.fixture
async def set_table() -> AsyncGenerator[None, Any]:
    """A SCHEMAFULL table whose field is a ``set<string>``, as ``set[str]`` migrates to."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        "REMOVE TABLE IF EXISTS i233_sets; DEFINE TABLE i233_sets SCHEMAFULL;"
        " DEFINE FIELD processed_by ON i233_sets TYPE option<set<string>>;"
    )
    await client.query("CREATE i233_sets:e1 SET processed_by = <set> ['pod-a'];")
    yield
    await client.query("REMOVE TABLE IF EXISTS i233_sets;")


async def _tags(record: str) -> list[str] | None:
    return (await I233Item.objects().get(record)).tags


async def _stored(thing: str, field: str) -> tuple[list[Any], str]:
    """The field's value and its SurrealDB type name (``set`` and ``array`` read alike)."""
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(f"SELECT VALUE [{field}, type::of({field})] FROM {thing};")
    value, type_name = response.all_records[0]
    return list(value), type_name


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
async def test_set_add_on_a_set_field_keeps_one_copy(set_table: None) -> None:
    """A ``set<string>`` column: ``array::add()`` alone rejects it, so every call raised."""
    await I233SetItem.atomic_set_add("e1", "processed_by", "pod-a")
    await I233SetItem.atomic_set_add("e1", "processed_by", "pod-b")
    records = await I233SetItem.atomic_set_add("e1", "processed_by", "pod-b")

    assert [list(r["processed_by"]) for r in records] == [["pod-a", "pod-b"]]
    assert await _stored("i233_sets:e1", "processed_by") == (["pod-a", "pod-b"], "set")


@pytest.mark.integration
async def test_set_add_keeps_an_untyped_set_a_set() -> None:
    """In a SCHEMALESS table nothing coerces the result back: the helper keeps the set."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("CREATE i233_items:set SET tags = <set> ['x'];")

    await I233Item.atomic_set_add("set", "tags", "x")
    await I233Item.atomic_set_add("set", "tags", ["y", "x"])

    assert await _stored("i233_items:set", "tags") == (["x", "y"], "set")


@pytest.mark.integration
async def test_set_add_keeps_an_array_an_array() -> None:
    await I233Item.atomic_set_add("tagged", "tags", ["p1", "x"])

    assert await _stored("i233_items:tagged", "tags") == (["x", "p1"], "array")


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
