"""Integration tests for #251: HTTP transactions against a real server.

``HTTPTransaction.commit()`` sends its statements as one ``BEGIN``/``COMMIT``
query. A statement the server rejects comes back as ``status: ERR`` inside a
successful response, the server cancels the rest of the batch and writes
nothing; ``commit()`` returned that response as if it had committed. It raises
``TransactionError`` now, with the rejected statement's own error.

The second half covers the write paths that go through ``tx.merge()`` over
HTTP — a single-field merge used to be rejected that way and so silently
dropped (``MERGE { k = $k }`` is not an object): direct ``tx.merge()`` and the
ORM's ``update(tx=)``, ``merge(tx=)`` and ``save(tx=)`` of a saved record.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.exceptions import TransactionError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = "test_issue_251"
ITEMS = "i251_items"


class I251Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=ITEMS)

    id: str | None = None
    amount: int | None = None
    label: str | None = None


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


@pytest.fixture
async def http() -> AsyncGenerator[HTTPConnection, None]:
    """A fresh table holding ``i251_items:keep`` (amount 1, label ``a``)."""
    conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    await conn.query(
        f"DEFINE NAMESPACE IF NOT EXISTS `{SURREALDB_NAMESPACE}`; DEFINE DATABASE IF NOT EXISTS `{SURREALDB_DATABASE}`;"
    )
    await conn.query(f"REMOVE TABLE IF EXISTS {ITEMS}; DEFINE TABLE {ITEMS} SCHEMALESS;")
    await conn.query(f"CREATE {ITEMS}:keep SET amount = 1, label = 'a';")
    yield conn
    await conn.query(f"REMOVE TABLE IF EXISTS {ITEMS};")
    await conn.close()


async def _row(conn: HTTPConnection, key: str) -> dict[str, Any] | None:
    records = (await conn.query(f"SELECT amount, label FROM {ITEMS}:{key};")).all_records
    return records[0] if records else None


# --- a rejected statement fails the commit -----------------------------------------


async def test_rejected_query_fails_the_commit(http: HTTPConnection) -> None:
    tx = http.transaction()

    with pytest.raises(TransactionError) as caught:
        async with tx:
            await tx.create(f"{ITEMS}:new", {"amount": 5})
            await tx.query("THROW 'i251 rejected';")

    assert "i251 rejected" in str(caught.value)
    assert "not executed" not in str(caught.value)
    assert await _row(http, "new") is None


async def test_rejected_create_reports_its_own_error(http: HTTPConnection) -> None:
    tx = http.transaction()
    async with tx:
        pass  # opened and committed empty: unchanged behaviour
    assert tx.is_committed is True

    tx = http.transaction()
    with pytest.raises(TransactionError) as caught:
        async with tx:
            await tx.create(f"{ITEMS}:new", {"amount": 5})
            await tx.create(f"{ITEMS}:keep", {"amount": 9})

    assert "already exists" in str(caught.value)
    assert "Cannot COMMIT" not in str(caught.value)
    assert await _row(http, "new") is None
    assert await _row(http, "keep") == {"amount": 1, "label": "a"}


async def test_state_after_a_rejected_statement(http: HTTPConnection) -> None:
    tx = http.transaction()
    await tx._begin()
    await tx.create(f"{ITEMS}:new", {"amount": 5})
    await tx.query("THROW 'i251 rejected';")
    assert http._open_transactions

    with pytest.raises(TransactionError):
        await tx.commit()

    assert tx.is_committed is False
    assert tx.is_active is False
    assert not http._open_transactions
    # The connection is still usable.
    assert await _row(http, "keep") == {"amount": 1, "label": "a"}


async def test_successful_batch_commits(http: HTTPConnection) -> None:
    tx = http.transaction()
    async with tx:
        await tx.create(f"{ITEMS}:new", {"amount": 5})
        await tx.query(f"UPDATE {ITEMS}:keep SET amount = $amount;", {"amount": 2})

    assert tx.is_committed is True
    assert not http._open_transactions
    assert await _row(http, "new") == {"amount": 5, "label": None}
    assert await _row(http, "keep") == {"amount": 2, "label": "a"}


# --- tx.merge() and the ORM paths built on it -------------------------------------


async def test_single_field_merge_changes_the_row(http: HTTPConnection) -> None:
    async with http.transaction() as tx:
        await tx.merge(f"{ITEMS}:keep", {"amount": 5})

    assert await _row(http, "keep") == {"amount": 5, "label": "a"}


async def test_multi_field_merge_changes_the_row(http: HTTPConnection) -> None:
    async with http.transaction() as tx:
        await tx.merge(f"{ITEMS}:keep", {"amount": 5, "label": "b"})

    assert await _row(http, "keep") == {"amount": 5, "label": "b"}


async def test_orm_update_in_transaction(http: HTTPConnection) -> None:
    item = await I251Item.objects().get("keep")
    item.amount = 7

    async with await SurrealDBConnectionManager.transaction() as tx:
        await item.update(tx=tx)

    assert await _row(http, "keep") == {"amount": 7, "label": "a"}


async def test_orm_merge_in_transaction(http: HTTPConnection) -> None:
    item = await I251Item.objects().get("keep")

    async with await SurrealDBConnectionManager.transaction() as tx:
        await item.merge(tx=tx, amount=8)

    assert await _row(http, "keep") == {"amount": 8, "label": "a"}


async def test_orm_save_of_a_saved_record_in_transaction(http: HTTPConnection) -> None:
    item = await I251Item.objects().get("keep")
    item.amount = 9
    item.label = "c"

    async with await SurrealDBConnectionManager.transaction() as tx:
        await item.save(tx=tx)

    assert await _row(http, "keep") == {"amount": 9, "label": "c"}
