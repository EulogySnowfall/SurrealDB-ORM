"""Unit tests for #446 (v2 line): only the server's retryable wording counts as a conflict.

``TransactionConflictError.is_conflict_error()`` matched a bare ``"conflict"``
(and ``"failed transaction"``, ``"document changed"``). A failed ``ASSERT``
echoes the rejected value and names the field, so a value such as
``'conflict-resolved'`` or a field named ``conflicts`` made a plain validation
failure a "retryable conflict": the atomic array helpers raised
``TransactionConflictError`` instead of ``QueryError``, and
``@retry_on_conflict`` retried a deterministic failure.

SurrealDB 2.7.0 reports a commit-time conflict on every statement of the
transaction, with the cancellation sentence in front of the retryable one.
``HTTPTransaction.commit()`` filed those under the cancellation cascade, so a
conflicting commit was raised as a plain ``TransactionError``.

The messages below are copied from SurrealDB 2.7.0, the pinned server.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.utils import retry_on_conflict
from surreal_sdk.exceptions import QueryError, TransactionConflictError, TransactionError
from surreal_sdk.transaction import HTTPTransaction
from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

ASSERT_VALUE_CONFLICT = (
    "Found ['p', 'conflict-resolved'] for field `tags`, with record `u446_items:a`, "
    "but field must conform to: array::len($value ?? []) < 2"
)
ASSERT_FIELD_CONFLICTS = (
    "Found [1, 2] for field `conflicts`, with record `u446_items:a`, but field must conform to: array::len($value ?? []) < 2"
)
# A write conflict, on a plain statement or on each statement of a BEGIN/COMMIT block.
WRITE_CONFLICT = (
    "The query was not executed due to a failed transaction. Failed to commit transaction "
    "due to a read or write conflict. This transaction can be retried"
)
NOT_EXECUTED = "The query was not executed due to a failed transaction"

ASSERT_MESSAGES = [ASSERT_VALUE_CONFLICT, ASSERT_FIELD_CONFLICTS]
HELPERS = ["atomic_append", "atomic_remove", "atomic_set_add"]
UPDATED = [{"id": "u446_items:a", "tags": ["p"]}]


class U446Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u446_items")

    id: str | None = None
    tags: list[str] | None = None


def _response(*statements: tuple[ResponseStatus, Any]) -> QueryResponse:
    return QueryResponse(results=[QueryResult(status=s, result=r) for s, r in statements], raw=[])


def _ok(result: Any = None) -> tuple[ResponseStatus, Any]:
    return (ResponseStatus.OK, result)


def _err(message: str) -> tuple[ResponseStatus, Any]:
    return (ResponseStatus.ERR, message)


def _client(*responses: QueryResponse) -> AsyncMock:
    client = AsyncMock()
    client.query = AsyncMock(side_effect=list(responses))
    return client


def _patch_client(client: AsyncMock) -> Any:
    return patch(
        "src.surreal_orm.model_base.SurrealDBConnectionManager.get_client",
        new_callable=AsyncMock,
        return_value=client,
    )


async def _open(response: QueryResponse) -> HTTPTransaction:
    conn = MagicMock()
    conn.query = AsyncMock(return_value=response)
    tx = HTTPTransaction(conn)
    await tx._begin()
    await tx.create("u446_items:a", {"x": 1})
    await tx.create("u446_items:b", {"x": 2})
    return tx


class TestMatcher:
    @pytest.mark.parametrize("message", ASSERT_MESSAGES)
    def test_assert_message_is_not_a_conflict(self, message: str) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception(message))

    def test_server_conflict_wording_is_a_conflict(self) -> None:
        assert TransactionConflictError.is_conflict_error(Exception(WRITE_CONFLICT))

    def test_server_conflict_wording_is_matched_case_insensitively(self) -> None:
        assert TransactionConflictError.is_conflict_error(Exception(WRITE_CONFLICT.upper()))

    def test_cancelled_statement_is_not_a_conflict(self) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception(NOT_EXECUTED))

    @pytest.mark.parametrize("message", ["Write conflict detected", "document changed", "failed transaction"])
    def test_loose_wording_is_not_a_conflict(self, message: str) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception(message))


class TestAtomicHelpers:
    @pytest.mark.parametrize("message", ASSERT_MESSAGES)
    @pytest.mark.parametrize("helper", HELPERS)
    async def test_assert_raises_query_error(self, helper: str, message: str) -> None:
        client = _client(_response(_err(message)))
        with _patch_client(client), pytest.raises(QueryError) as caught:
            await getattr(U446Item, helper)("a", "tags", "conflict-resolved")

        assert not isinstance(caught.value, TransactionConflictError)
        assert message in str(caught.value)

    @pytest.mark.parametrize("message", ASSERT_MESSAGES)
    async def test_retry_on_conflict_does_not_retry_an_assert(self, message: str) -> None:
        client = _client(_response(_err(message)), _response(_ok(UPDATED)))

        @retry_on_conflict(max_retries=3, base_delay=0.001)
        async def claim() -> list[dict[str, Any]]:
            return await U446Item.atomic_set_add("a", "tags", "conflict-resolved")

        with _patch_client(client), pytest.raises(QueryError) as caught:
            await claim()

        assert not isinstance(caught.value, TransactionConflictError)
        assert client.query.await_count == 1

    async def test_real_conflict_is_still_retried(self) -> None:
        client = _client(_response(_err(WRITE_CONFLICT)), _response(_ok(UPDATED)))

        @retry_on_conflict(max_retries=3, base_delay=0.001)
        async def claim() -> list[dict[str, Any]]:
            return await U446Item.atomic_set_add("a", "tags", "q")

        with _patch_client(client):
            assert await claim() == UPDATED

        assert client.query.await_count == 2


class TestHTTPTransactionCommit:
    async def test_conflict_on_every_statement_raises_transaction_conflict_error(self) -> None:
        tx = await _open(_response(_err(WRITE_CONFLICT), _err(WRITE_CONFLICT)))

        with pytest.raises(TransactionConflictError, match="can be retried"):
            await tx.commit()

    async def test_assert_cause_after_cancelled_statements_is_not_a_conflict(self) -> None:
        tx = await _open(_response(_err(NOT_EXECUTED), _err(ASSERT_VALUE_CONFLICT)))

        with pytest.raises(TransactionError, match="conflict-resolved") as caught:
            await tx.commit()

        assert not isinstance(caught.value, TransactionConflictError)

    async def test_only_cancelled_statements_is_not_a_conflict(self) -> None:
        tx = await _open(_response(_err(NOT_EXECUTED), _err(NOT_EXECUTED)))

        with pytest.raises(TransactionError) as caught:
            await tx.commit()

        assert not isinstance(caught.value, TransactionConflictError)

    async def test_retry_on_conflict_retries_a_conflicting_commit(self) -> None:
        conn = MagicMock()
        conn.query = AsyncMock(side_effect=[_response(_err(WRITE_CONFLICT)), _response(_ok())])

        @retry_on_conflict(max_retries=2, base_delay=0.001, max_delay=0.001)
        async def write() -> HTTPTransaction:
            tx = HTTPTransaction(conn)
            async with tx:
                await tx.create("u446_items:a", {"x": 1})
            return tx

        tx = await write()

        assert tx.is_committed is True
        assert conn.query.await_count == 2
