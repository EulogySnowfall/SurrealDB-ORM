"""Integration tests for #263: a failed ``ASSERT`` mentioning "conflict" is a ``QueryError``.

SurrealDB echoes the rejected value and names the field in an ``ASSERT``
failure. Before #263 the SDK's conflict matcher read the word "conflict" in that
message as a retryable conflict.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.utils import retry_on_conflict
from surreal_sdk.exceptions import QueryError, TransactionConflictError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_263"

pytestmark = pytest.mark.integration


class I263Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i263_items")

    id: str | None = None
    tags: list[str] | None = None
    conflicts: list[int] | None = None


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, Any]:
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
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        "REMOVE TABLE IF EXISTS i263_items; DEFINE TABLE i263_items SCHEMALESS;"
        " DEFINE FIELD tags ON i263_items TYPE option<array<string>> ASSERT array::len($value ?? []) < 2;"
        " DEFINE FIELD conflicts ON i263_items TYPE option<array<int>> ASSERT array::len($value ?? []) < 2;"
    )
    await client.query("CREATE i263_items:a SET tags = ['p'], conflicts = [1];")
    yield
    await client.query("REMOVE TABLE IF EXISTS i263_items;")


async def test_assert_with_conflict_in_the_value_raises_query_error() -> None:
    with pytest.raises(QueryError, match="conflict-resolved") as caught:
        await I263Item.atomic_set_add("a", "tags", "conflict-resolved")

    assert not isinstance(caught.value, TransactionConflictError)


async def test_assert_on_a_field_named_conflicts_raises_query_error() -> None:
    with pytest.raises(QueryError, match="`conflicts`") as caught:
        await I263Item.atomic_append("a", "conflicts", 2)

    assert not isinstance(caught.value, TransactionConflictError)


async def test_retry_on_conflict_runs_the_assert_once() -> None:
    calls = 0

    @retry_on_conflict(max_retries=3, base_delay=0.001)
    async def claim() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return await I263Item.atomic_set_add("a", "tags", "conflict-resolved")

    with pytest.raises(QueryError) as caught:
        await claim()

    assert not isinstance(caught.value, TransactionConflictError)
    assert calls == 1
