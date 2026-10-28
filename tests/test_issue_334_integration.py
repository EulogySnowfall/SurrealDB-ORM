"""Integration tests for #334: field names in ``HTTPTransaction`` statements.

Keys that are identifiers commit as before over HTTP. A key that is not raises
``ValueError`` when the statement is queued: the batch sent at ``commit()``
never contains it, the target table holds the same rows and ``INFO FOR DB``
is unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.protocol.cbor import RecordId
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = "test_issue_334"

NODES = "i334_nodes"
LINKS = "i334_links"

BAD_KEYS = ["a b", "a-b", "a;b", "v = 1; CREATE i334_nodes:extra SET v"]


@pytest.fixture
async def http() -> AsyncGenerator[HTTPConnection, None]:
    conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    await conn.query(
        f"DEFINE NAMESPACE IF NOT EXISTS `{SURREALDB_NAMESPACE}`; DEFINE DATABASE IF NOT EXISTS `{SURREALDB_DATABASE}`;"
    )
    await conn.query(
        f"REMOVE TABLE IF EXISTS {NODES}; REMOVE TABLE IF EXISTS {LINKS};"
        f"DEFINE TABLE {LINKS} TYPE RELATION;"
        f"CREATE {NODES}:keep SET v = 0; CREATE {NODES}:other SET v = 0;"
    )
    yield conn
    await conn.close()


async def _info(conn: HTTPConnection) -> Any:
    info = (await conn.query("INFO FOR DB;")).first_result
    assert info is not None
    return info.result


async def _rows(conn: HTTPConnection) -> list[Any]:
    result = await conn.query(f"SELECT * FROM {NODES}, {LINKS};")
    return sorted(result.all_records, key=repr)


async def test_valid_keys_commit(http: HTTPConnection) -> None:
    async with http.transaction() as tx:
        await tx.create(f"{NODES}:x", {"v": 1, "name": "x"})
        await tx.merge(f"{NODES}:keep", {"m": 2})
        await tx.relate(f"{NODES}:x", LINKS, f"{NODES}:keep", {"w": 3})

    assert (await http.query(f"SELECT * FROM {NODES}:x;")).all_records == [
        {"id": RecordId(table=NODES, id="x"), "v": 1, "name": "x"}
    ]
    assert (await http.query(f"SELECT * FROM {NODES}:keep;")).all_records == [
        {"id": RecordId(table=NODES, id="keep"), "v": 0, "m": 2}
    ]
    edges = (await http.query(f"SELECT in, out, w FROM {LINKS};")).all_records
    assert edges == [{"in": RecordId(table=NODES, id="x"), "out": RecordId(table=NODES, id="keep"), "w": 3}]


@pytest.mark.parametrize("key", BAD_KEYS)
@pytest.mark.parametrize("op", ["create", "merge", "relate"])
async def test_invalid_key_sends_nothing(http: HTTPConnection, op: str, key: str) -> None:
    info = await _info(http)
    rows = await _rows(http)
    sent: list[str] = []
    query = http.query

    async def spy(sql: str, vars: dict[str, Any] | None = None) -> Any:
        sent.append(sql)
        return await query(sql, vars)

    http.query = spy  # type: ignore[method-assign]
    try:
        with pytest.raises(ValueError, match="expected an identifier"):
            async with http.transaction() as tx:
                await tx.create(f"{NODES}:before", {"v": 1})
                if op == "create":
                    await tx.create(f"{NODES}:y", {key: 1})
                elif op == "merge":
                    await tx.merge(f"{NODES}:keep", {key: 1})
                else:
                    await tx.relate(f"{NODES}:keep", LINKS, f"{NODES}:other", {key: 1})
    finally:
        http.query = query  # type: ignore[method-assign]

    # The exception rolls the transaction back: nothing at all reaches the server.
    assert sent == []
    assert await _rows(http) == rows
    assert len(await _rows(http)) == len(rows)
    assert await _info(http) == info
