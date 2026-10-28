"""Unit tests for #382: field names in queued ``HTTPTransaction`` statements.

``HTTPTransaction.create``/``insert``/``update``/``upsert``/``merge``/``relate``
render each key of ``data`` into the queued SurrealQL twice, as the field name
and as the name of its parameter (``k = $k``). A key that is not a plain
identifier — a space, a ``-``, a ``;`` — produced a statement that only failed
at ``commit()``, far from the call that queued it, or one that did not parse
the way it reads.

Each key is checked now when the statement is queued: anything other than an
identifier raises ``ValueError`` naming the key, and nothing is queued. Keys
that are identifiers render exactly as before.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.surreal_sdk.transaction import HTTPTransaction
from src.surreal_sdk.types import QueryResponse

#: Keys that are not plain SurrealQL identifiers.
BAD_KEYS: list[Any] = ["a b", "a-b", "a;b", "v = 1; x", "1a", "", "a.b", "$a", "a`b", "é", "a\n", 1]

DataOp = Callable[[HTTPTransaction, dict[str, Any]], Awaitable[Any]]

#: One queued call per method that renders ``data`` keys into the statement.
OPS: dict[str, DataOp] = {
    "create": lambda tx, d: tx.create("i382_nodes:x", d),
    "insert": lambda tx, d: tx.insert("i382_nodes", d),
    "insert_list": lambda tx, d: tx.insert("i382_nodes", [{"ok": 1}, d]),
    "update": lambda tx, d: tx.update("i382_nodes:x", d),
    "upsert": lambda tx, d: tx.upsert("i382_nodes:x", d),
    "merge": lambda tx, d: tx.merge("i382_nodes:x", d),
    "relate": lambda tx, d: tx.relate("i382_nodes:x", "i382_links", "i382_nodes:y", d),
}


@pytest.fixture
def connection() -> MagicMock:
    conn = MagicMock()
    conn.query = AsyncMock(return_value=QueryResponse(results=[], raw=[]))
    return conn


async def _open(conn: MagicMock) -> HTTPTransaction:
    tx = HTTPTransaction(conn)
    await tx._begin()
    return tx


def re_key(key: str) -> str:
    return re.escape(repr(key))


class TestInvalidKeys:
    @pytest.mark.parametrize("op", OPS)
    @pytest.mark.parametrize("key", BAD_KEYS, ids=repr)
    async def test_raises_at_the_call_naming_the_key(self, connection: MagicMock, op: str, key: Any) -> None:
        tx = await _open(connection)
        with pytest.raises(ValueError) as excinfo:
            await OPS[op](tx, {"ok": 1, key: 2})
        assert repr(key) in str(excinfo.value)
        assert tx._statements == []

    @pytest.mark.parametrize("op", OPS)
    async def test_nothing_is_sent_on_commit(self, connection: MagicMock, op: str) -> None:
        tx = await _open(connection)
        await tx.create("i382_nodes:before", {"v": 1})
        with pytest.raises(ValueError, match=re_key("a b")):
            await OPS[op](tx, {"a b": 1})
        assert len(tx._statements) == 1
        await tx.commit()
        sql = connection.query.await_args.args[0]
        assert "a b" not in sql
        assert sql.count("CREATE") == 1

    async def test_insert_checks_every_record_before_queueing(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        with pytest.raises(ValueError, match=re_key("a-b")):
            await tx.insert("i382_nodes", [{"v": 1}, {"v": 2}, {"a-b": 3}])
        assert tx._statements == []


class TestValidKeysUnchanged:
    async def test_create(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.create("i382_nodes:x", {"a": 1, "_b2": 2, "Cc": 3})
        assert tx._statements[0].sql == "CREATE i382_nodes:x SET a = $a, _b2 = $_b2, Cc = $Cc;"
        assert tx._statements[0].vars == {"a": 1, "_b2": 2, "Cc": 3}

    async def test_create_without_data(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.create("i382_nodes:x")
        await tx.create("i382_nodes:y", {})
        assert [s.sql for s in tx._statements] == ["CREATE i382_nodes:x;", "CREATE i382_nodes:y;"]

    async def test_insert(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.insert("i382_nodes", [{"a": 1}, {"a": 2, "b": 3}])
        assert [(s.sql, s.vars) for s in tx._statements] == [
            ("CREATE i382_nodes SET a = $r0_a;", {"r0_a": 1}),
            ("CREATE i382_nodes SET a = $r1_a, b = $r1_b;", {"r1_a": 2, "r1_b": 3}),
        ]

    async def test_update_upsert_merge(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.update("i382_nodes:x", {"a": 1, "b": 2})
        await tx.upsert("i382_nodes:x", {"a": 1})
        await tx.merge("i382_nodes:x", {"a": 1, "b": 2})
        assert [s.sql for s in tx._statements] == [
            "UPDATE i382_nodes:x SET a = $a, b = $b;",
            "UPSERT i382_nodes:x SET a = $a;",
            "UPDATE i382_nodes:x MERGE { a: $a, b: $b };",
        ]

    async def test_relate(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.relate("i382_nodes:x", "i382_links", "i382_nodes:y", {"w": 1})
        await tx.relate("i382_nodes:x", "i382_links", "i382_nodes:y")
        assert [s.sql for s in tx._statements] == [
            "RELATE i382_nodes:x->i382_links->i382_nodes:y SET w = $w;",
            "RELATE i382_nodes:x->i382_links->i382_nodes:y;",
        ]

    async def test_commit_namespaces_variables(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.create("i382_nodes:x", {"a": 1})
        await tx.merge("i382_nodes:x", {"a": 2, "auth": 3})
        await tx.commit()
        sql, variables = connection.query.await_args.args
        assert sql == (
            "BEGIN TRANSACTION;\n"
            "CREATE i382_nodes:x SET a = $tx_0_a;\n"
            "UPDATE i382_nodes:x MERGE { a: $tx_1_a, auth: $tx_1_auth };\n"
            "COMMIT TRANSACTION;"
        )
        assert variables == {"tx_0_a": 1, "tx_1_a": 2, "tx_1_auth": 3}
