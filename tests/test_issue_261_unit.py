"""Unit tests for #261: the atomic array helpers surface statement errors (v2 line).

Backport of #257. ``client.query()`` raises only on an RPC-level failure. A
statement SurrealDB rejects — a write conflict, a failed ``ASSERT``, an argument
of the wrong type — comes back inside a *successful* RPC as ``status: ERR``.
``atomic_append``, ``atomic_remove`` and ``atomic_set_add`` used to read only the
records of that response, so a rejected write returned ``[]`` like a no-op, and
``@retry_on_conflict`` never saw the conflict it exists to retry.

The client is mocked at the SDK boundary: each test hands the helper the
``QueryResponse`` a rejected (or accepted) statement produces.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.utils import retry_on_conflict
from surreal_sdk.exceptions import QueryError, SurrealDBError, TransactionConflictError
from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

#: What SurrealDB 2.7.0 reports for a failed ``ASSERT``.
ASSERT_MESSAGE = (
    "Found ['x', 'p1'] for field `tags`, with record `i261_unit_items:a`, but field must conform to: $value.len() == 1"
)

#: What SurrealDB reports for a write conflict between two transactions.
CONFLICT_MESSAGE = "Failed to commit transaction due to a read or write conflict. This transaction can be retried"

UPDATED = [{"id": "i261_unit_items:a", "tags": ["x", "p1"]}]

HELPERS = ["atomic_append", "atomic_remove", "atomic_set_add"]


class I261UnitItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i261_unit_items")

    id: str | None = None
    tags: list[str] | None = None


def _err(message: str) -> QueryResponse:
    """The response of one statement that SurrealDB rejected."""
    return QueryResponse(results=[QueryResult(status=ResponseStatus.ERR, result=message)])


def _ok(records: list[dict[str, Any]]) -> QueryResponse:
    """The response of one ``UPDATE`` that SurrealDB applied."""
    return QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=records)])


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


class TestStatementErrorRaises:
    @pytest.mark.parametrize("helper", HELPERS)
    async def test_statement_error_raises_with_server_message(self, helper: str) -> None:
        """A rejected statement raises QueryError with the server's message and the query."""
        client = _client(_err(ASSERT_MESSAGE))
        with _patch_client(client), pytest.raises(SurrealDBError) as excinfo:
            await getattr(I261UnitItem, helper)("a", "tags", "p1")

        assert isinstance(excinfo.value, QueryError)
        assert not isinstance(excinfo.value, TransactionConflictError)
        assert ASSERT_MESSAGE in str(excinfo.value)
        assert excinfo.value.query is not None
        assert "UPDATE i261_unit_items:a" in excinfo.value.query

    @pytest.mark.parametrize("helper", HELPERS)
    async def test_conflict_error_raises_transaction_conflict(self, helper: str) -> None:
        """A write conflict raises the retryable TransactionConflictError."""
        client = _client(_err(CONFLICT_MESSAGE))
        with _patch_client(client), pytest.raises(TransactionConflictError) as excinfo:
            await getattr(I261UnitItem, helper)("a", "tags", "p1")

        assert CONFLICT_MESSAGE in str(excinfo.value)

    @pytest.mark.parametrize("helper", HELPERS)
    async def test_error_in_any_statement_raises(self, helper: str) -> None:
        """Every statement of the response is checked, not only the first."""
        response = QueryResponse(
            results=[
                QueryResult(status=ResponseStatus.OK, result=UPDATED),
                QueryResult(status=ResponseStatus.ERR, result=ASSERT_MESSAGE),
            ]
        )
        client = _client(response)
        with _patch_client(client), pytest.raises(QueryError, match="must conform to"):
            await getattr(I261UnitItem, helper)("a", "tags", "p1")

    @pytest.mark.parametrize("helper", HELPERS)
    async def test_accepted_statement_returns_records(self, helper: str) -> None:
        """The happy path is unchanged: the updated records come back."""
        client = _client(_ok(UPDATED))
        with _patch_client(client):
            records = await getattr(I261UnitItem, helper)("a", "tags", "p1")

        assert records == UPDATED

    @pytest.mark.parametrize("helper", HELPERS)
    async def test_missing_record_returns_empty_list(self, helper: str) -> None:
        """An UPDATE on no record is not an error: still an empty list."""
        client = _client(_ok([]))
        with _patch_client(client):
            assert await getattr(I261UnitItem, helper)("nope", "tags", "p1") == []


class TestRetryOnConflict:
    async def test_conflict_from_atomic_set_add_is_retried(self) -> None:
        """@retry_on_conflict retries the conflict and returns the eventual success."""
        client = _client(_err(CONFLICT_MESSAGE), _ok(UPDATED))

        @retry_on_conflict(max_retries=2, base_delay=0.001)
        async def claim() -> list[dict[str, Any]]:
            return await I261UnitItem.atomic_set_add("a", "tags", "p1")

        with _patch_client(client):
            records = await claim()

        assert records == UPDATED
        assert client.query.await_count == 2

    async def test_non_conflict_error_is_not_retried(self) -> None:
        """Any other statement error propagates on the first attempt."""
        client = _client(_err(ASSERT_MESSAGE), _ok(UPDATED))

        @retry_on_conflict(max_retries=2, base_delay=0.001)
        async def claim() -> list[dict[str, Any]]:
            return await I261UnitItem.atomic_set_add("a", "tags", "p1")

        with _patch_client(client), pytest.raises(QueryError, match="must conform to"):
            await claim()

        assert client.query.await_count == 1


class TestEmittedQueries:
    @pytest.mark.parametrize(
        ("helper", "expected"),
        [
            # array::append() rejects NONE, so a missing field starts from [].
            ("atomic_append", "UPDATE i261_unit_items:a SET tags = array::append(tags ?? [], $value);"),
            ("atomic_remove", "UPDATE i261_unit_items:a SET tags -= $value;"),
            # += on an array appends duplicates; array::add() adds only absent values.
            ("atomic_set_add", "UPDATE i261_unit_items:a SET tags = array::add(tags ?? [], $value);"),
        ],
    )
    async def test_emitted_query(self, helper: str, expected: str) -> None:
        client = _client(_ok(UPDATED))
        with _patch_client(client):
            await getattr(I261UnitItem, helper)("a", "tags", "p1")

        query, variables = client.query.await_args.args
        assert query == expected
        assert variables == {"value": "p1"}

    @pytest.mark.parametrize("helper", HELPERS)
    async def test_no_set_type_check(self, helper: str) -> None:
        """``type::is_set`` (main's set branch) is a parse error on SurrealDB 2.x."""
        client = _client(_ok(UPDATED))
        with _patch_client(client):
            await getattr(I261UnitItem, helper)("a", "tags", "p1")

        query, _ = client.query.await_args.args
        assert "type::is_set" not in query
        assert "<set>" not in query
        assert "<array>" not in query
