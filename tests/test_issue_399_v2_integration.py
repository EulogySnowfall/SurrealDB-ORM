"""Integration tests for #399 (v2 line, SurrealDB 2.x): HTTP transactions against a real server.

``HTTPTransaction.commit()`` sends its statements as one ``BEGIN``/``COMMIT``
query. A statement the server rejects comes back as ``status: ERR`` inside a
successful response, the server cancels the rest of the batch and writes
nothing; ``commit()`` returned that response as if it had committed. It raises
``TransactionError`` now, with the rejected statement's own error.

A successful batch, an empty transaction, the ORM write paths and WebSocket
transactions behave as before.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.exceptions import TransactionConflictError, TransactionError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER, SURREALDB_WS_URL

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = "test_issue_399_v2"
ITEMS = "i399_items"


class I399Item(BaseSurrealModel):
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
    """A fresh SCHEMAFULL table holding ``i399_items:keep`` (amount 1, label ``a``)."""
    conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    await conn.query(
        f"DEFINE NAMESPACE IF NOT EXISTS `{SURREALDB_NAMESPACE}`; DEFINE DATABASE IF NOT EXISTS `{SURREALDB_DATABASE}`;"
    )
    await conn.query(
        f"REMOVE TABLE IF EXISTS {ITEMS}; DEFINE TABLE {ITEMS} SCHEMAFULL;"
        f" DEFINE FIELD amount ON {ITEMS} TYPE option<int>;"
        f" DEFINE FIELD label ON {ITEMS} TYPE option<string>;"
    )
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
            await tx.query("THROW 'i399 rejected';")

    assert "i399 rejected" in str(caught.value)
    assert "not executed" not in str(caught.value)
    assert not isinstance(caught.value, TransactionConflictError)
    assert await _row(http, "new") is None


async def test_rejected_type_reports_its_own_error(http: HTTPConnection) -> None:
    tx = http.transaction()

    with pytest.raises(TransactionError) as caught:
        async with tx:
            await tx.create(f"{ITEMS}:new", {"amount": 5})
            await tx.query(f"UPDATE {ITEMS}:keep SET amount = $amount;", {"amount": "not a number"})
            await tx.create(f"{ITEMS}:other", {"amount": 6})

    assert "expected a" in str(caught.value)
    assert "not executed" not in str(caught.value)
    assert not isinstance(caught.value, TransactionConflictError)
    assert await _row(http, "new") is None
    assert await _row(http, "other") is None
    assert await _row(http, "keep") == {"amount": 1, "label": "a"}


async def test_rejected_create_reports_its_own_error(http: HTTPConnection) -> None:
    tx = http.transaction()

    with pytest.raises(TransactionError) as caught:
        async with tx:
            await tx.create(f"{ITEMS}:new", {"amount": 5})
            await tx.create(f"{ITEMS}:keep", {"amount": 9})

    assert "already exists" in str(caught.value)
    assert await _row(http, "new") is None
    assert await _row(http, "keep") == {"amount": 1, "label": "a"}


async def test_state_after_a_rejected_statement(http: HTTPConnection) -> None:
    tx = http.transaction()
    await tx._begin()
    await tx.create(f"{ITEMS}:new", {"amount": 5})
    await tx.query("THROW 'i399 rejected';")

    with pytest.raises(TransactionError):
        await tx.commit()

    assert tx.is_committed is False
    assert tx.is_active is False
    # The connection is still usable.
    assert await _row(http, "keep") == {"amount": 1, "label": "a"}


async def test_orm_save_in_a_rejected_transaction_writes_nothing(http: HTTPConnection) -> None:
    item = I399Item(id="saved", amount=3, label="s")

    with pytest.raises(TransactionError, match="i399 rejected"):
        async with await SurrealDBConnectionManager.transaction() as tx:
            await item.save(tx=tx)
            await tx.query("THROW 'i399 rejected';")

    assert await _row(http, "saved") is None


# --- unchanged behaviour ------------------------------------------------------------


async def test_empty_transaction_commits(http: HTTPConnection) -> None:
    tx = http.transaction()
    async with tx:
        pass

    assert tx.is_committed is True


async def test_successful_batch_commits(http: HTTPConnection) -> None:
    tx = http.transaction()
    async with tx:
        await tx.create(f"{ITEMS}:new", {"amount": 5})
        await tx.query(f"UPDATE {ITEMS}:keep SET amount = $amount;", {"amount": 2})

    assert tx.is_committed is True
    assert await _row(http, "new") == {"amount": 5, "label": None}
    assert await _row(http, "keep") == {"amount": 2, "label": "a"}


async def test_orm_writes_in_a_successful_transaction(http: HTTPConnection) -> None:
    item = await I399Item.objects().get("keep")
    created = I399Item(id="saved", amount=3, label="s")

    async with await SurrealDBConnectionManager.transaction() as tx:
        await created.save(tx=tx)
        await item.merge(tx=tx, amount=8)

    assert tx.is_committed is True
    assert await _row(http, "saved") == {"amount": 3, "label": "s"}
    assert await _row(http, "keep") == {"amount": 8, "label": "a"}


async def test_websocket_transaction_is_unchanged(http: HTTPConnection) -> None:
    ws = WebSocketConnection(SURREALDB_WS_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await ws.connect()
    try:
        await ws.signin(SURREALDB_USER, SURREALDB_PASS)
        tx = ws.transaction()
        async with tx:
            await tx.create(f"{ITEMS}:ws", {"amount": 4})

        assert tx.is_committed is True
    finally:
        await ws.close()

    assert await _row(http, "ws") == {"amount": 4, "label": None}
