"""Integration tests for #261: the atomic array helpers against SurrealDB 2.x.

Backport of #257. ``atomic_set_add`` was documented as a set-like add but ran
``SET f += $value``, which appends on an array: two calls with the same value
left two copies. It now runs ``array::add(f ?? [], $value)``. On 2.x a set is
an array, so the same query covers an ``option<set<T>>`` field, unset or not.

``atomic_append`` starts from ``[]`` too: ``array::append`` rejects a missing
field, and that rejection used to be swallowed, so the value was never written.
A statement the server rejects now raises instead of returning ``[]``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk.exceptions import QueryError, TransactionConflictError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_261_v2"


class I261Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i261_items")

    id: str | None = None
    tags: list[str] | None = None


class I261SetItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i261_sets")

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
    await client.query("REMOVE TABLE IF EXISTS i261_items; DEFINE TABLE i261_items SCHEMALESS;")
    await client.query("CREATE i261_items:tagged SET tags = ['x']; CREATE i261_items:bare;")
    yield
    await client.query("REMOVE TABLE IF EXISTS i261_items;")


@pytest.fixture
async def set_table() -> AsyncGenerator[None, Any]:
    """A SCHEMAFULL ``option<set<string>>`` field: one record unset, one holding a value."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        "REMOVE TABLE IF EXISTS i261_sets; DEFINE TABLE i261_sets SCHEMAFULL;"
        " DEFINE FIELD processed_by ON i261_sets TYPE option<set<string>>;"
    )
    await client.query("CREATE i261_sets:unset; CREATE i261_sets:held SET processed_by = ['pod-a'];")
    yield
    await client.query("REMOVE TABLE IF EXISTS i261_sets;")


async def _tags(record: str) -> list[str] | None:
    return (await I261Item.objects().get(record)).tags


async def _processed_by(record: str) -> Any:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(f"SELECT VALUE processed_by FROM i261_sets:{record};")
    return response.all_records[0]


# ---------------------------------------------------------------- atomic_set_add


@pytest.mark.integration
async def test_set_add_twice_keeps_one_copy() -> None:
    await I261Item.atomic_set_add("tagged", "tags", "p1")
    records = await I261Item.atomic_set_add("tagged", "tags", "p1")

    assert await _tags("tagged") == ["x", "p1"]
    assert [r["tags"] for r in records] == [["x", "p1"]]


@pytest.mark.integration
async def test_set_add_present_value_is_a_no_op() -> None:
    await I261Item.atomic_set_add("tagged", "tags", "x")

    assert await _tags("tagged") == ["x"]


@pytest.mark.integration
async def test_set_add_on_missing_field_twice_keeps_one_copy() -> None:
    await I261Item.atomic_set_add("bare", "tags", "p1")
    await I261Item.atomic_set_add("bare", "tags", "p1")

    assert await _tags("bare") == ["p1"]


@pytest.mark.integration
async def test_set_add_list_adds_only_missing_elements() -> None:
    """A list value is spread: each element is added if absent (``+=`` appended them all)."""
    await I261Item.atomic_set_add("tagged", "tags", ["p1", "x"])

    assert await _tags("tagged") == ["x", "p1"]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("record", "expected"),
    [("unset", ["pod-b"]), ("held", ["pod-a", "pod-b"])],
)
async def test_set_add_on_a_set_field_keeps_one_copy(set_table: None, record: str, expected: list[str]) -> None:
    """``option<set<string>>``, unset and holding values: ``array::add`` works on both."""
    await I261SetItem.atomic_set_add(record, "processed_by", "pod-b")
    records = await I261SetItem.atomic_set_add(record, "processed_by", "pod-b")

    assert [r["processed_by"] for r in records] == [expected]
    assert await _processed_by(record) == expected


# ------------------------------------------------------------------ atomic_append


@pytest.mark.integration
async def test_append_on_missing_field_creates_the_array() -> None:
    records = await I261Item.atomic_append("bare", "tags", "p1")

    assert await _tags("bare") == ["p1"]
    assert [r["tags"] for r in records] == [["p1"]]


@pytest.mark.integration
async def test_append_twice_keeps_both_copies() -> None:
    await I261Item.atomic_append("tagged", "tags", "p1")
    await I261Item.atomic_append("tagged", "tags", "p1")

    assert await _tags("tagged") == ["x", "p1", "p1"]


# ------------------------------------------------------------------ atomic_remove


@pytest.mark.integration
async def test_remove_drops_the_value() -> None:
    await I261Item.atomic_append("tagged", "tags", "p1")
    records = await I261Item.atomic_remove("tagged", "tags", "x")

    assert await _tags("tagged") == ["p1"]
    assert [r["tags"] for r in records] == [["p1"]]


@pytest.mark.integration
async def test_remove_on_missing_field_leaves_it_unset() -> None:
    await I261Item.atomic_remove("bare", "tags", "x")

    assert await _tags("bare") is None


# ------------------------------------------------------------------ all three


@pytest.mark.integration
@pytest.mark.parametrize(
    ("helper", "value"),
    [("atomic_append", "p1"), ("atomic_set_add", "p1"), ("atomic_remove", "x")],
)
async def test_rejected_statement_raises(helper: str, value: str) -> None:
    """A write the server rejects raises with its message; the record is unchanged."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("DEFINE FIELD OVERWRITE tags ON i261_items TYPE option<array<string>> ASSERT $value.len() == 1;")

    with pytest.raises(QueryError, match="must conform to") as excinfo:
        await getattr(I261Item, helper)("tagged", "tags", value)

    assert not isinstance(excinfo.value, TransactionConflictError)
    assert "tags" in str(excinfo.value)
    assert excinfo.value.query is not None
    assert await _tags("tagged") == ["x"]


@pytest.mark.integration
@pytest.mark.parametrize("helper", ["atomic_append", "atomic_remove", "atomic_set_add"])
async def test_missing_record_returns_empty_list(helper: str) -> None:
    assert await getattr(I261Item, helper)("nope", "tags", "x") == []
