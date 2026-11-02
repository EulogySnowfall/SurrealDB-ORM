"""Unit tests for #263: only the server's retryable wording counts as a conflict.

``TransactionConflictError.is_conflict_error()`` matched a bare ``"conflict"``
(and ``"failed transaction"``, ``"document changed"``). A failed ``ASSERT``
echoes the rejected value and names the field, so a value such as
``'conflict-resolved'`` or a field named ``conflicts`` made a plain validation
failure a "retryable conflict": the atomic array helpers raised
``TransactionConflictError`` instead of ``QueryError``, and
``@retry_on_conflict`` retried a deterministic failure.

The messages below are copied from SurrealDB 3.3.5, the pinned server.
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
    "Found ['p', 'conflict-resolved'] for field `tags`, with record `u263_items:a`, "
    "but field must conform to: array::len($value) < 2"
)
ASSERT_FIELD_CONFLICTS = "Found 9 for field `conflicts`, with record `u263_items:a`, but field must conform to: $value < 5"
KV_WRITE_CONFLICT = (
    "There was a problem with the key-value store: Transaction conflict: Write conflict, "
    "retry the transaction. This transaction can be retried"
)
COMMIT_WRITE_CONFLICT = (
    "Cannot COMMIT: Transaction conflict: Write conflict, retry the transaction. This transaction can be retried"
)
NOT_EXECUTED = "The query was not executed due to a failed transaction"
COMMIT_ABORTED = "Cannot COMMIT: the transaction was aborted due to a prior error"

ASSERT_MESSAGES = [ASSERT_VALUE_CONFLICT, ASSERT_FIELD_CONFLICTS]
HELPERS = ["atomic_append", "atomic_remove", "atomic_set_add"]
UPDATED = [{"id": "u263_items:a", "tags": ["p"]}]


class U263Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u263_items")

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


class TestMatcher:
    @pytest.mark.parametrize("message", ASSERT_MESSAGES)
    def test_assert_message_is_not_a_conflict(self, message: str) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception(message))

    @pytest.mark.parametrize("message", [KV_WRITE_CONFLICT, COMMIT_WRITE_CONFLICT])
    def test_server_conflict_wording_is_a_conflict(self, message: str) -> None:
        assert TransactionConflictError.is_conflict_error(Exception(message))

    @pytest.mark.parametrize("message", [NOT_EXECUTED, COMMIT_ABORTED])
    def test_cascade_message_is_not_a_conflict(self, message: str) -> None:
        assert not TransactionConflictError.is_conflict_error(Exception(message))


class TestAtomicHelpers:
    @pytest.mark.parametrize("message", ASSERT_MESSAGES)
    @pytest.mark.parametrize("helper", HELPERS)
    async def test_assert_raises_query_error(self, helper: str, message: str) -> None:
        client = _client(_response(_err(message)))
        with _patch_client(client), pytest.raises(QueryError) as caught:
            await getattr(U263Item, helper)("a", "tags", "conflict-resolved")

        assert not isinstance(caught.value, TransactionConflictError)
        assert message in str(caught.value)

    @pytest.mark.parametrize("message", ASSERT_MESSAGES)
    async def test_retry_on_conflict_does_not_retry_an_assert(self, message: str) -> None:
        client = _client(_response(_err(message)), _response(_ok(UPDATED)))

        @retry_on_conflict(max_retries=3, base_delay=0.001)
        async def claim() -> list[dict[str, Any]]:
            return await U263Item.atomic_set_add("a", "tags", "conflict-resolved")

        with _patch_client(client), pytest.raises(QueryError) as caught:
            await claim()

        assert not isinstance(caught.value, TransactionConflictError)
        assert client.query.await_count == 1

    async def test_real_conflict_is_still_retried(self) -> None:
        client = _client(_response(_err(KV_WRITE_CONFLICT)), _response(_ok(UPDATED)))

        @retry_on_conflict(max_retries=3, base_delay=0.001)
        async def claim() -> list[dict[str, Any]]:
            return await U263Item.atomic_set_add("a", "tags", "p")

        with _patch_client(client):
            assert await claim() == UPDATED
        assert client.query.await_count == 2


async def _commit(response: QueryResponse) -> None:
    conn = MagicMock()
    conn.query = AsyncMock(return_value=response)
    tx = HTTPTransaction(conn)
    await tx._begin()
    await tx.create("u263_items:a", {"x": 1})
    await tx.create("u263_items:b", {"x": 2})
    await tx.commit()


class TestHTTPTransactionCommit:
    async def test_conflict_reported_on_commit_is_a_conflict(self) -> None:
        """3.3.5 reports a commit-time conflict on the COMMIT, after the cascade messages."""
        response = _response(_ok(), _err(NOT_EXECUTED), _err(NOT_EXECUTED), _err(COMMIT_WRITE_CONFLICT))

        with pytest.raises(TransactionConflictError, match="Write conflict"):
            await _commit(response)

    @pytest.mark.parametrize("message", ASSERT_MESSAGES)
    async def test_assert_in_a_transaction_is_not_a_conflict(self, message: str) -> None:
        response = _response(_ok(), _err(NOT_EXECUTED), _err(message), _err(COMMIT_ABORTED))

        with pytest.raises(TransactionError) as caught:
            await _commit(response)

        assert not isinstance(caught.value, TransactionConflictError)
        assert message in str(caught.value)

    async def test_cancellation_only_is_not_a_conflict(self) -> None:
        response = _response(_ok(), _err(NOT_EXECUTED), _err(COMMIT_ABORTED))

        with pytest.raises(TransactionError) as caught:
            await _commit(response)

        assert not isinstance(caught.value, TransactionConflictError)
