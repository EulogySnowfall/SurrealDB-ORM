"""Unit tests for #503: non-atomic ``bulk_update()`` / ``bulk_delete()`` raise on a rejected statement.

``client.query()`` raises only on an RPC-level failure.  A statement SurrealDB
rejects (a failed ``ASSERT``, an event that throws, a write conflict) comes
back inside a *successful* RPC as ``status: ERR`` with no records, so the
plain bulk paths returned ``0`` — the same answer as a filter that matched
nothing — and a conflict never reached ``@retry_on_conflict``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.surreal_orm.cache import QueryCache
from src.surreal_orm.connection_manager import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from surreal_sdk.exceptions import QueryError, TransactionConflictError
from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

_CONFLICT = "Failed to commit transaction due to a read or write conflict. This transaction can be retried"


class U503Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u503_items")

    id: str | None = None
    name: str = ""
    age: int = 0


def _response(status: ResponseStatus, result: Any) -> QueryResponse:
    return QueryResponse(results=[QueryResult(status=status, result=result)])


def _client(response: QueryResponse) -> AsyncMock:
    client = AsyncMock()
    client.query = AsyncMock(return_value=response)
    return client


_WRITES: dict[str, Callable[[], Awaitable[int]]] = {
    "bulk_update": lambda: U503Item.objects().filter(age__gte=1).bulk_update({"name": "n"}),
    "bulk_delete": lambda: U503Item.objects().filter(age__gte=1).bulk_delete(),
}


@pytest.fixture(autouse=True)
def _cache_state() -> Iterator[None]:
    """A cache holding one entry for this table and one for another table."""
    saved = (QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size)
    QueryCache.clear()
    QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size = True, 60, 1000
    QueryCache.set("mine", ["stale"], "u503_items")
    QueryCache.set("other", ["kept"], "u503_others")
    try:
        yield
    finally:
        QueryCache.clear()
        QueryCache._enabled, QueryCache._default_ttl, QueryCache._max_size = saved


@pytest.mark.parametrize("write", list(_WRITES))
class TestRejectedStatementRaises:
    async def test_rejection_raises_query_error(self, write: str) -> None:
        response = _response(ResponseStatus.ERR, "Found -5 for field `age`, but field must conform to: $value >= 0")
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client(response))),
            pytest.raises(QueryError, match="must conform") as info,
        ):
            await _WRITES[write]()
        assert not isinstance(info.value, TransactionConflictError)
        assert info.value.query is not None
        assert "u503_items" in info.value.query

    async def test_rejection_leaves_the_cache(self, write: str) -> None:
        response = _response(ResponseStatus.ERR, "rejected")
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client(response))),
            pytest.raises(QueryError),
        ):
            await _WRITES[write]()
        assert QueryCache.get("mine") == ["stale"]
        assert QueryCache.get("other") == ["kept"]

    async def test_conflict_raises_transaction_conflict_error(self, write: str) -> None:
        response = _response(ResponseStatus.ERR, _CONFLICT)
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client(response))),
            pytest.raises(TransactionConflictError),
        ):
            await _WRITES[write]()
        assert QueryCache.get("mine") == ["stale"]

    async def test_success_returns_the_count_and_invalidates(self, write: str) -> None:
        records = [{"id": "u503_items:a", "name": "n"}, {"id": "u503_items:b", "name": "n"}]
        response = _response(ResponseStatus.OK, records)
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client(response))):
            assert await _WRITES[write]() == 2
        assert QueryCache.get("mine") is None
        assert QueryCache.get("other") == ["kept"]

    async def test_nothing_matched_still_returns_zero(self, write: str) -> None:
        response = _response(ResponseStatus.OK, [])
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=_client(response))):
            assert await _WRITES[write]() == 0
