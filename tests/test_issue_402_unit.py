"""Unit tests for #402: "cancelled transaction" is part of a failed transaction's cascade.

Once a statement fails a ``BEGIN``/``COMMIT`` batch, SurrealDB reports the
statements before it as "not executed due to a failed transaction", the
statements after it as "not executed due to a cancelled transaction", and the
``COMMIT`` as aborted. Only the first and last wordings were recognised, so a
"cancelled transaction" message read first would have been reported as the
cause. The server puts the cause before those statements today, which is why
the results did not change; the check no longer depends on that order.

Server messages copied from SurrealDB 3.3.5 (the pin) and 3.2.6, replaying
``BEGIN; CREATE ...; THROW 'boom'; CREATE ...; COMMIT;``: both servers report the
same four messages.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.surreal_orm.utils import retry_on_conflict
from surreal_sdk.exceptions import ChangeFeedError, TransactionConflictError, TransactionError
from surreal_sdk.streaming.change_feed import ChangeFeedStream, _statement_error
from surreal_sdk.transaction import HTTPTransaction, _failed_statement_error, _is_cancelled_statement_error
from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

NOT_EXECUTED = "The query was not executed due to a failed transaction"
CANCELLED = "The query was not executed due to a cancelled transaction"
ABORTED = "Cannot COMMIT: the transaction was aborted due to a prior error"
CAUSE = "An error occurred: boom"


def _response(*statements: tuple[ResponseStatus, Any]) -> QueryResponse:
    return QueryResponse(results=[QueryResult(status=s, result=r) for s, r in statements], raw=[])


def _ok(result: Any = None) -> tuple[ResponseStatus, Any]:
    return (ResponseStatus.OK, result)


def _err(message: str) -> tuple[ResponseStatus, Any]:
    return (ResponseStatus.ERR, message)


class TestCancelledTransactionMarker:
    def test_cancelled_transaction_is_a_cascade_message(self) -> None:
        assert _is_cancelled_statement_error(CANCELLED)

    def test_cause_is_not_a_cascade_message(self) -> None:
        assert not _is_cancelled_statement_error(CAUSE)

    def test_server_order_still_reports_the_cause(self) -> None:
        response = _response(_ok(), _err(NOT_EXECUTED), _err(CAUSE), _err(CANCELLED), _err(ABORTED))

        assert _failed_statement_error(response) == (CAUSE, True)

    def test_cancelled_before_the_cause_still_reports_the_cause(self) -> None:
        response = _response(_ok(), _err(CANCELLED), _err(CAUSE), _err(ABORTED))

        assert _failed_statement_error(response) == (CAUSE, True)

    def test_only_cascade_messages_report_no_cause(self) -> None:
        response = _response(_ok(), _err(CANCELLED), _err(NOT_EXECUTED), _err(ABORTED))

        assert _failed_statement_error(response) == (CANCELLED, False)


async def _commit(response: QueryResponse) -> MagicMock:
    """Queue two statements on an ``HTTPTransaction`` answered by *response*, then commit."""
    conn = MagicMock()
    conn.query = AsyncMock(return_value=response)
    tx = HTTPTransaction(conn)
    await tx._begin()
    await tx.create("i402_items:a", {"x": 1})
    await tx.create("i402_items:b", {"x": 2})
    await tx.commit()
    return conn


class TestCommit:
    @pytest.mark.asyncio
    async def test_cancelled_before_the_cause_raises_the_cause(self) -> None:
        with pytest.raises(TransactionError) as caught:
            await _commit(_response(_ok(), _err(CANCELLED), _err(CAUSE), _err(ABORTED)))

        assert CAUSE in str(caught.value)
        assert CANCELLED not in str(caught.value)
        assert not isinstance(caught.value, TransactionConflictError)

    @pytest.mark.asyncio
    async def test_only_cancellation_messages_is_not_a_conflict(self) -> None:
        with pytest.raises(TransactionError) as caught:
            await _commit(_response(_ok(), _err(CANCELLED), _err(CANCELLED), _err(ABORTED)))

        assert not isinstance(caught.value, TransactionConflictError)
        assert not TransactionConflictError.is_conflict_error(caught.value)

    @pytest.mark.asyncio
    async def test_retry_on_conflict_makes_one_attempt(self) -> None:
        response = _response(_ok(), _err(CANCELLED), _err(CANCELLED), _err(ABORTED))
        attempts = 0

        @retry_on_conflict(max_retries=3, base_delay=0.001)
        async def work() -> None:
            nonlocal attempts
            attempts += 1
            await _commit(response)

        with pytest.raises(TransactionError) as caught:
            await work()

        assert not isinstance(caught.value, TransactionConflictError)
        assert attempts == 1


class _CancelledFirstConnection:
    """Answers every query with a failed transaction whose cancelled statement is listed first."""

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        return QueryResponse.from_rpc_result(
            [
                {"status": "OK", "result": None},
                {"status": "ERR", "result": CANCELLED},
                {"status": "ERR", "result": CAUSE},
                {"status": "ERR", "result": ABORTED},
            ]
        )


class TestChangeFeed:
    def test_statement_error_skips_the_cancelled_statement(self) -> None:
        response = _response(_ok(), _err(CANCELLED), _err(CAUSE), _err(ABORTED))

        assert _statement_error(response) == CAUSE

    @pytest.mark.asyncio
    async def test_reader_reports_the_cause(self) -> None:
        feed = ChangeFeedStream(_CancelledFirstConnection(), "i402_orders")  # type: ignore[arg-type]

        with pytest.raises(ChangeFeedError) as excinfo:
            await feed.get_changes(since=0)

        assert CAUSE in str(excinfo.value)
        assert CANCELLED not in str(excinfo.value)
