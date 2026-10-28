"""Unit tests for #251: ``HTTPTransaction.commit()`` checks each statement's status.

A statement the server rejects does not fail the HTTP request: it comes back as
``status: ERR`` inside a successful response, the server cancels every other
statement of the batch and writes nothing. ``commit()`` used to return that
response as if the transaction had committed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.surreal_sdk.exceptions import TransactionConflictError, TransactionError
from src.surreal_sdk.transaction import HTTPTransaction, WebSocketTransaction
from src.surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

CANCELLED = "The query was not executed due to a failed transaction"
ABORTED = "Cannot COMMIT: the transaction was aborted due to a prior error"


def _response(*statements: tuple[ResponseStatus, Any]) -> QueryResponse:
    return QueryResponse(results=[QueryResult(status=s, result=r) for s, r in statements], raw=[])


def _ok(result: Any = None) -> tuple[ResponseStatus, Any]:
    return (ResponseStatus.OK, result)


def _err(message: str) -> tuple[ResponseStatus, Any]:
    return (ResponseStatus.ERR, message)


async def _open(response: QueryResponse) -> tuple[HTTPTransaction, MagicMock]:
    conn = MagicMock()
    conn.query = AsyncMock(return_value=response)
    tx = HTTPTransaction(conn)
    await tx._begin()
    await tx.create("i251_items:a", {"x": 1})
    await tx.create("i251_items:a", {"x": 2})
    return tx, conn


async def test_rejected_statement_raises_with_its_own_error() -> None:
    response = _response(_ok(), _err(CANCELLED), _err("Database record `i251_items:a` already exists"), _err(ABORTED))
    tx, _ = await _open(response)

    with pytest.raises(TransactionError) as caught:
        await tx.commit()

    assert "already exists" in str(caught.value)
    assert "not executed" not in str(caught.value)
    assert "Cannot COMMIT" not in str(caught.value)
    assert not isinstance(caught.value, TransactionConflictError)


async def test_cause_is_found_after_the_cancelled_statements() -> None:
    response = _response(_ok(), _err(CANCELLED), _err(CANCELLED), _err("An error occurred: boom"), _err(ABORTED))
    tx, _ = await _open(response)

    with pytest.raises(TransactionError, match="boom"):
        await tx.commit()


async def test_state_after_a_rejected_statement() -> None:
    tx, _ = await _open(_response(_ok(), _err("boom"), _err(ABORTED)))

    with pytest.raises(TransactionError):
        await tx.commit()

    assert tx.is_committed is False
    assert tx.is_active is False
    with pytest.raises(TransactionError, match="not active"):
        await tx.commit()


async def test_retryable_conflict_raises_transaction_conflict_error() -> None:
    message = "Transaction conflict: Resource busy. This transaction can be retried"
    tx, _ = await _open(_response(_ok(), _err(message), _err(CANCELLED), _err(ABORTED)))

    with pytest.raises(TransactionConflictError, match="Resource busy"):
        await tx.commit()


async def test_only_cancellation_messages_is_not_a_conflict() -> None:
    tx, _ = await _open(_response(_ok(), _err(CANCELLED), _err(ABORTED)))

    with pytest.raises(TransactionError) as caught:
        await tx.commit()

    assert not isinstance(caught.value, TransactionConflictError)


async def test_successful_batch_returns_the_response_unchanged() -> None:
    response = _response(_ok(), _ok([{"id": "i251_items:a"}]), _ok([{"id": "i251_items:b"}]), _ok())
    tx, conn = await _open(response)

    assert await tx.commit() is response
    assert tx.is_committed is True
    assert tx.is_active is False
    conn.query.assert_awaited_once()


async def test_empty_transaction_does_not_query() -> None:
    conn = MagicMock()
    conn.query = AsyncMock()
    tx = HTTPTransaction(conn)
    await tx._begin()

    response = await tx.commit()

    assert response.results == []
    assert tx.is_committed is True
    conn.query.assert_not_awaited()


async def test_request_failure_still_raises_transaction_error() -> None:
    conn = MagicMock()
    conn.query = AsyncMock(side_effect=RuntimeError("Parse error"))
    tx = HTTPTransaction(conn)
    await tx._begin()
    await tx.query("SELECT * FROM i251_items WHERE ;")

    with pytest.raises(TransactionError, match="Parse error"):
        await tx.commit()
    assert tx.is_committed is False
    assert tx.is_active is False


async def test_context_manager_propagates_the_rejection() -> None:
    conn = MagicMock()
    conn.query = AsyncMock(return_value=_response(_ok(), _err("boom"), _err(ABORTED)))
    tx = HTTPTransaction(conn)

    with pytest.raises(TransactionError, match="boom"):
        async with tx:
            await tx.create("i251_items:a", {"x": 1})


async def test_websocket_commit_is_unchanged() -> None:
    conn = MagicMock()
    conn.query = AsyncMock(return_value=_response(_ok()))
    tx = WebSocketTransaction(conn)
    await tx._begin()

    await tx.commit()

    assert tx.is_committed is True
    assert [call.args[0] for call in conn.query.await_args_list] == ["BEGIN TRANSACTION;", "COMMIT TRANSACTION;"]
