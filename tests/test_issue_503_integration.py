"""Integration tests for #503: a write the server refuses makes the plain bulk paths raise.

A failed ``ASSERT`` (``bulk_update()``) or an event that throws on delete
(``bulk_delete()``) is reported as ``status: ERR`` inside a successful RPC.
The non-atomic bulk paths read only the records, so both used to return ``0``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, _raise_on_statement_error
from surreal_sdk.exceptions import QueryError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

_DATABASE = "test_issue_503"


class I503Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i503_items")

    id: str | None = None
    age: int = 0


@pytest.fixture(scope="module", autouse=True)
async def _connection() -> AsyncGenerator[None, None]:
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        _DATABASE,
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def _schema() -> None:
    """Rebuild the table; a rejected statement fails here, not later as an ``_ages()`` mismatch."""
    client = await SurrealDBConnectionManager.get_client()
    query = (
        "REMOVE TABLE IF EXISTS i503_items;"
        " DEFINE TABLE i503_items SCHEMAFULL;"
        " DEFINE FIELD age ON i503_items TYPE int ASSERT $value >= 0;"
        " CREATE i503_items:a SET age = 1; CREATE i503_items:b SET age = 2;"
    )
    _raise_on_statement_error(await client.query(query), query)


async def _ages() -> list[int]:
    client = await SurrealDBConnectionManager.get_client()
    result = await client.query("SELECT age FROM i503_items ORDER BY age;")
    return [row["age"] for row in result.all_records]


async def test_bulk_update_assert_violation_raises() -> None:
    with pytest.raises(QueryError):
        await I503Item.objects().filter(age__gte=0).bulk_update({"age": -5})
    assert await _ages() == [1, 2]


async def test_bulk_update_accepted_returns_count() -> None:
    assert await I503Item.objects().filter(age__gte=2).bulk_update({"age": 3}) == 1
    assert await _ages() == [1, 3]


async def test_bulk_delete_refused_by_event_raises() -> None:
    client = await SurrealDBConnectionManager.get_client()
    await client.query("DEFINE EVENT i503_no_delete ON i503_items WHEN $event = 'DELETE' THEN { THROW 'deletion refused' };")
    with pytest.raises(QueryError, match="deletion refused"):
        await I503Item.objects().filter(age__gte=0).bulk_delete()
    assert await _ages() == [1, 2]


async def test_bulk_delete_accepted_returns_count() -> None:
    assert await I503Item.objects().filter(age__gte=2).bulk_delete() == 1
    assert await _ages() == [1]
