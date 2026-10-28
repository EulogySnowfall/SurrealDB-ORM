r"""Unit tests for #306: record references in SDK transactions.

``HTTPTransaction`` queued ``create``/``update``/``upsert``/``merge``/``delete``/
``relate`` statements with the ``table:id`` reference written into the SQL as
given. The only check, ``_validate_thing()``, accepted any string containing a
``:``, so an ID with a space, a ``-``, a ``;``, a backtick, a backslash or a
leading digit produced a statement that failed to parse — or, for ``a-b``, one
that parsed as an expression and wrote nothing.

The reference is now parsed strictly: the table must be an identifier, and an
ID that is not a plain identifier (or integer, or ``rand()``/``ulid()``/
``uuid()``) is bound as a parameter through ``type::record()``, never written
into the SQL text. A backtick-delimited ID — the form ``format_thing()``
writes — is read back first, so it is bound once, not escaped twice.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.surreal_orm.utils import format_thing
from src.surreal_sdk.transaction import HTTPTransaction, WebSocketTransaction
from src.surreal_sdk.types import QueryResponse

#: IDs that are not plain identifiers: each must reach the server as data.
RAW_IDS = [
    "a b",
    "a`b",
    "dir\\",
    "a-b",
    "p;q",
    "7abc",
    "x; DELETE i306_nodes; --",
    "a\\`b",
    "$zz",
    "123",  # only through format_thing(): the backtick form is a string ID
    "x\n",
    "9223372036854775808",  # above the i64 range: a string ID, as the server reads it inline
    "-9223372036854775809",  # below the i64 range
    "+5",
    "05",
]
#: IDs that may be written as they are (the queued SQL is unchanged for them).
PLAIN_IDS = ["abc", "_x1", "A_b_9"]

Op = Callable[[HTTPTransaction, str], Awaitable[Any]]

#: One queued call per method that takes a record reference.
OPS: dict[str, Op] = {
    "create": lambda tx, t: tx.create(t, {"v": 1}),
    "create_no_data": lambda tx, t: tx.create(t),
    "update": lambda tx, t: tx.update(t, {"v": 1}),
    "upsert": lambda tx, t: tx.upsert(t, {"v": 1}),
    "merge": lambda tx, t: tx.merge(t, {"v": 1}),
    "delete": lambda tx, t: tx.delete(t),
    "relate_from": lambda tx, t: tx.relate(t, "i306_links", "i306_nodes:other", {"w": 2}),
    "relate_to": lambda tx, t: tx.relate("i306_nodes:other", "i306_links", t),
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


def _references(sql: str) -> set[str]:
    return set(re.findall(r"\$(\w+)", sql))


class TestQueuedStatement:
    @pytest.mark.parametrize("op", OPS)
    @pytest.mark.parametrize("raw", [r for r in RAW_IDS if r != "123"])
    async def test_raw_id_is_bound_not_written(self, connection: MagicMock, op: str, raw: str) -> None:
        tx = await _open(connection)
        await OPS[op](tx, f"i306_nodes:{raw}")
        stmt = tx._statements[-1]
        assert raw not in stmt.sql
        bound = [k for k, v in stmt.vars.items() if v == raw]
        assert len(bound) == 1
        assert f"type::record('i306_nodes', ${bound[0]})" in stmt.sql
        assert _references(stmt.sql) == set(stmt.vars)

    @pytest.mark.parametrize("op", OPS)
    @pytest.mark.parametrize("raw", RAW_IDS)
    async def test_format_thing_form_is_bound_once(self, connection: MagicMock, op: str, raw: str) -> None:
        """The backtick form is read back, so the bound value is the raw ID (no double escaping)."""
        tx = await _open(connection)
        await OPS[op](tx, format_thing("i306_nodes", raw))
        stmt = tx._statements[-1]
        assert raw in stmt.vars.values()
        assert "`" not in stmt.sql
        assert _references(stmt.sql) == set(stmt.vars)

    @pytest.mark.parametrize("op", OPS)
    @pytest.mark.parametrize("plain", PLAIN_IDS)
    async def test_plain_id_is_written_as_before(self, connection: MagicMock, op: str, plain: str) -> None:
        tx = await _open(connection)
        await OPS[op](tx, f"i306_nodes:{plain}")
        assert f"i306_nodes:{plain}" in tx._statements[-1].sql

    async def test_integer_and_generated_ids_are_written_as_before(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.update("i306_nodes:1", {"k": 1})
        await tx.create("i306_nodes:ulid()", {"k": 1})
        assert tx._statements[0].sql == "UPDATE i306_nodes:1 SET k = $k;"
        assert tx._statements[1].sql == "CREATE i306_nodes:ulid() SET k = $k;"

    @pytest.mark.parametrize("op", OPS)
    @pytest.mark.parametrize("integer", ["0", "-5", "1234567890123456789", "9223372036854775807", "-9223372036854775808"])
    async def test_i64_integer_id_is_written_inline(self, connection: MagicMock, op: str, integer: str) -> None:
        """SurrealQL reads these inline as integer IDs; binding them as strings would miss the record."""
        tx = await _open(connection)
        await OPS[op](tx, f"i306_nodes:{integer}")
        stmt = tx._statements[-1]
        assert f"i306_nodes:{integer}" in stmt.sql
        assert integer not in stmt.vars.values()
        assert "type::record" not in stmt.sql

    async def test_table_only_reference_is_written_as_before(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.create("i306_nodes", {"k": 1})
        await tx.delete("i306_nodes")
        assert tx._statements[0].sql == "CREATE i306_nodes SET k = $k;"
        assert tx._statements[1].sql == "DELETE i306_nodes;"

    async def test_binding_name_avoids_data_keys(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.upsert("i306_nodes:a b", {"_ref": "data", "v": 1})
        stmt = tx._statements[-1]
        assert stmt.vars["_ref"] == "data"
        assert "a b" in stmt.vars.values()
        assert _references(stmt.sql) == set(stmt.vars)

    async def test_relate_binds_both_ends_separately(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.relate("i306_nodes:a b", "i306_links", "i306_nodes:c-d")
        stmt = tx._statements[-1]
        assert sorted(stmt.vars.values()) == ["a b", "c-d"]
        assert stmt.sql.startswith("RELATE (type::record('i306_nodes', $")
        assert "->i306_links->(type::record('i306_nodes', $" in stmt.sql

    async def test_commit_renames_reference_bindings(self, connection: MagicMock) -> None:
        """A ``$name`` inside the ID cannot be renamed by commit(): it is not in the SQL text."""
        tx = await _open(connection)
        await tx.update("i306_nodes:x$v", {"v": 1})
        await tx.update("i306_nodes:y z", {"v": 2})
        await tx.commit()
        sent_sql, sent_vars = connection.query.call_args[0]
        assert _references(sent_sql) == set(sent_vars)
        assert sorted(v for v in sent_vars.values() if isinstance(v, str)) == ["x$v", "y z"]


class TestMalformedReferences:
    @pytest.mark.parametrize("op", OPS)
    @pytest.mark.parametrize(
        "thing",
        [
            ":x",
            "a b:c",
            "",
            "i306_nodes:",
            "7tbl:x",
            "i3-06:x",
            "`i306_nodes`:x",
            "i306_nodes;x",
            "i306_nodes:`unterminated",
            "i306_nodes:`a`b`",
            "i306_nodes:[1, 2]",
            "i306_nodes:{a: 1}",
            "i306_nodes:⟨a⟩b⟩",
            "i306_nodes\n:x",
            "i306_nodes\n",
            "\ni306_nodes:x",
        ],
    )
    async def test_raises_before_queueing(self, connection: MagicMock, op: str, thing: str) -> None:
        tx = await _open(connection)
        with pytest.raises(ValueError):
            await OPS[op](tx, thing)
        assert tx._statements == []

    @pytest.mark.parametrize("relation", ["", "a b", "x->y", "rel;", "7rel", "rel:id", "`rel`", "rel\n", "i306_links\n"])
    async def test_relation_must_be_an_identifier(self, connection: MagicMock, relation: str) -> None:
        tx = await _open(connection)
        with pytest.raises(ValueError):
            await tx.relate("i306_nodes:a", relation, "i306_nodes:b")
        assert tx._statements == []

    @pytest.mark.parametrize("table", ["", "a b", "i306_nodes:x", "t;", "7t", "`t`", "t\n", "i306_nodes\n"])
    async def test_insert_table_must_be_an_identifier(self, connection: MagicMock, table: str) -> None:
        tx = await _open(connection)
        with pytest.raises(ValueError):
            await tx.insert(table, {"v": 1})
        assert tx._statements == []

    async def test_angle_bracket_id_is_read_back(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        await tx.delete("i306_nodes:⟨a b⟩")
        assert "a b" in tx._statements[-1].vars.values()


class TestWebSocketTransaction:
    """The WebSocket transaction passes references to RPC calls, where they are data."""

    @pytest.fixture
    def ws_connection(self) -> MagicMock:
        conn = MagicMock()
        for name in ("query", "create", "insert", "update", "upsert", "merge", "delete", "relate"):
            setattr(conn, name, AsyncMock(return_value=None))
        return conn

    async def _open(self, conn: MagicMock) -> WebSocketTransaction:
        tx = WebSocketTransaction(conn)
        await tx._begin()
        return tx

    @pytest.mark.parametrize("thing", [":x", "a b:c", "", "i306_nodes:"])
    async def test_malformed_reference_raises_before_the_call(self, ws_connection: MagicMock, thing: str) -> None:
        tx = await self._open(ws_connection)
        for call in (
            tx.create(thing),
            tx.update(thing, {}),
            tx.upsert(thing, {}),
            tx.merge(thing, {}),
            tx.delete(thing),
            tx.relate(thing, "i306_links", "i306_nodes:b"),
        ):
            with pytest.raises(ValueError):
                await call
        for name in ("create", "update", "upsert", "merge", "delete", "relate"):
            getattr(ws_connection, name).assert_not_called()

    async def test_relation_and_table_are_identifiers(self, ws_connection: MagicMock) -> None:
        tx = await self._open(ws_connection)
        with pytest.raises(ValueError):
            await tx.relate("i306_nodes:a", "x->y", "i306_nodes:b")
        with pytest.raises(ValueError):
            await tx.insert("t;", {"v": 1})
        ws_connection.relate.assert_not_called()
        ws_connection.insert.assert_not_called()

    @pytest.mark.parametrize("raw", RAW_IDS)
    async def test_valid_reference_is_passed_through(self, ws_connection: MagicMock, raw: str) -> None:
        tx = await self._open(ws_connection)
        thing = format_thing("i306_nodes", raw)
        await tx.delete(thing)
        ws_connection.delete.assert_awaited_once_with(thing)
