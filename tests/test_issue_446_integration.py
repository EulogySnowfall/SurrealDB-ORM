"""Integration tests for #446 (v2 line): what counts as a retryable conflict on SurrealDB 2.7.

A failed ``ASSERT`` echoes the rejected value and names the field. Before #446
the SDK's conflict matcher read the word "conflict" in that message as a
retryable conflict. And a conflict detected when a ``BEGIN``/``COMMIT`` block
commits is reported, on 2.7, on every statement with the cancellation sentence
in front of it, so ``HTTPTransaction.commit()`` raised it as a plain
``TransactionError``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.utils import retry_on_conflict
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.exceptions import QueryError, TransactionConflictError, TransactionError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_446_v2"
ITEMS = "i446_items"

pytestmark = pytest.mark.integration


class I446Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=ITEMS)

    id: str | None = None
    tags: list[str] | None = None
    conflicts: list[int] | None = None


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
    """``i446_items:a`` with one tag, one conflict and a counter; both arrays hold at most one item."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        f"REMOVE TABLE IF EXISTS {ITEMS}; DEFINE TABLE {ITEMS} SCHEMALESS;"
        f" DEFINE FIELD tags ON {ITEMS} TYPE option<array<string>> ASSERT array::len($value ?? []) < 2;"
        f" DEFINE FIELD conflicts ON {ITEMS} TYPE option<array<int>> ASSERT array::len($value ?? []) < 2;"
    )
    await client.query(f"CREATE {ITEMS}:a SET tags = ['p'], conflicts = [1], n = 0;")
    yield
    await client.query(f"REMOVE TABLE IF EXISTS {ITEMS};")


async def test_assert_with_conflict_in_the_value_raises_query_error() -> None:
    with pytest.raises(QueryError, match="conflict-resolved") as caught:
        await I446Item.atomic_set_add("a", "tags", "conflict-resolved")

    assert not isinstance(caught.value, TransactionConflictError)


async def test_assert_on_a_field_named_conflicts_raises_query_error() -> None:
    with pytest.raises(QueryError, match="`conflicts`") as caught:
        await I446Item.atomic_append("a", "conflicts", 2)

    assert not isinstance(caught.value, TransactionConflictError)


async def test_retry_on_conflict_runs_the_assert_once() -> None:
    calls = 0

    @retry_on_conflict(max_retries=3, base_delay=0.001)
    async def claim() -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        return await I446Item.atomic_set_add("a", "tags", "conflict-resolved")

    with pytest.raises(QueryError) as caught:
        await claim()

    assert not isinstance(caught.value, TransactionConflictError)
    assert calls == 1


async def test_conflicting_commit_raises_transaction_conflict_error() -> None:
    """Concurrent transactions on one record: every failed commit is a ``TransactionConflictError``."""
    conns = []
    for _ in range(4):
        conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
        await conn.connect()
        await conn.signin(SURREALDB_USER, SURREALDB_PASS)
        conns.append(conn)

    async def bump(conn: HTTPConnection) -> None:
        async with conn.transaction() as tx:
            await tx.query(f"UPDATE {ITEMS}:a SET n += 1;")
            await tx.query("SLEEP 300ms;")

    failures: list[BaseException] = []
    try:
        # The overlapping commits conflict on the first round in practice; a few
        # rounds keep the test independent of scheduling.
        for _ in range(5):
            results = await asyncio.gather(*(bump(conn) for conn in conns), return_exceptions=True)
            failures.extend(r for r in results if isinstance(r, BaseException))
            if failures:
                break
    finally:
        for conn in conns:
            await conn.close()

    assert failures, "no concurrent commit conflicted"
    for failure in failures:
        assert isinstance(failure, TransactionConflictError), repr(failure)
        assert isinstance(failure, TransactionError)
        assert "can be retried" in str(failure)
