"""#454: JSON Lines export pages by record-id range, not by a keyset predicate.

After the first batch, each statement reads ``FROM $_x_after``, bound as the
record-id range that starts right after the previous batch's last id (CBOR tag
8 around a tag 49 range, tag 51 excluded start, open end). The checks here are
deterministic: ``EXPLAIN`` of every statement the export sends after the first
batch is a ``RecordIdScan`` with no ``TableScan`` and no sort, for every kind of
record id and through the ORM (filter + ``LET`` prelude); and the export
returns every record exactly once in ``ORDER BY id`` order, for several batch
sizes, with ``limit=`` and ``where=``, on an edge table, and when the cursor
record is deleted between two batches.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

import pytest
from cbor2 import frozendict

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.subquery import Subquery
from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.jsonl import loads_record
from surreal_sdk.protocol.cbor import RecordId

pytestmark = pytest.mark.integration

URL = "http://localhost:8000"
NS = "test"
DB = "test_issue_454"
MIXED = "i454_mixed"
EDGE = "i454_edge"
DOOMED = "i454_doomed"

# Every kind of record id, in no particular order: numbers, strings (one that
# looks like a number, one with a space), UUIDs, arrays and objects.
KEYS = [
    "3",
    "alice",
    "-7",
    "u'0290f3b2-7c1e-7a4b-9f00-123456789abc'",
    "[2]",
    "{ b: 2 }",
    "⟨42⟩",
    "1",
    "u'0190f3b2-7c1e-7a4b-9f00-123456789abc'",
    "[1, 'a']",
    "bob",
    "{ a: 1 }",
    "⟨7 x⟩",
    "2",
]


async def _open(kind: str = "http") -> BaseSurrealConnection:
    connection: BaseSurrealConnection
    if kind == "http":
        connection = HTTPConnection(URL, NS, DB)
    else:
        connection = WebSocketConnection(URL, NS, DB, timeout=10, auto_reconnect=False)
    await connection.connect()
    await connection.signin("root", "root")
    await connection.query(f"DEFINE NAMESPACE IF NOT EXISTS {NS}; DEFINE DATABASE IF NOT EXISTS {DB};")
    await connection.use(NS, DB)
    return connection


async def _fill(conn: BaseSurrealConnection, table: str) -> None:
    await conn.query(f"REMOVE TABLE IF EXISTS {table};")
    for v, key in enumerate(KEYS):
        response = await conn.query(f"CREATE {table}:{key} SET v = {v}, even = {str(v % 2 == 0).lower()};")
        assert response.is_ok, response.raw


@pytest.fixture(scope="module")
async def tables() -> AsyncGenerator[None, None]:
    """``i454_mixed`` (one row per id in ``KEYS``) and ``i454_edge`` (five edges between them)."""
    conn = await _open()
    try:
        await _fill(conn, MIXED)
        await conn.query(f"REMOVE TABLE IF EXISTS {EDGE};")
        for left, right in [("1", "2"), ("2", "3"), ("alice", "bob"), ("[2]", "{ a: 1 }"), ("-7", "alice")]:
            response = await conn.query(f"RELATE {MIXED}:{left}->{EDGE}->{MIXED}:{right};")
            assert response.is_ok, response.raw
        yield
        await conn.query(f"REMOVE TABLE IF EXISTS {MIXED}; REMOVE TABLE IF EXISTS {EDGE}; REMOVE TABLE IF EXISTS {DOOMED};")
    finally:
        await conn.close()


@pytest.fixture(params=["http", "ws"])
async def conn(request: pytest.FixtureRequest, tables: None) -> AsyncGenerator[BaseSurrealConnection, None]:
    connection = await _open(request.param)
    try:
        yield connection
    finally:
        await connection.close()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Every ``(sql, vars)`` an ``HTTPConnection`` sends while the test runs."""
    calls: list[tuple[str, dict[str, Any]]] = []
    original = HTTPConnection.query

    async def recording(self: HTTPConnection, sql: str, vars: dict[str, Any] | None = None) -> Any:
        calls.append((sql, dict(vars or {})))
        return await original(self, sql, vars)

    monkeypatch.setattr(HTTPConnection, "query", recording)
    return calls


async def _ordered_ids(conn: BaseSurrealConnection, table: str, where: str = "") -> list[RecordId]:
    response = await conn.query(f"SELECT VALUE id FROM {table} {where} ORDER BY id;")
    return list(response.results[-1].result)


def _plain(value: Any) -> Any:
    """A record id as lists and dicts: a key read from a line is a ``list`` / ``dict``, from the server a ``tuple`` /
    ``frozendict``; both bind back to the same key."""
    if isinstance(value, RecordId):
        return ("record", value.table, _plain(value.id))
    if isinstance(value, dict | frozendict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _operators(plan: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "operator" in node:
                found.append(str(node["operator"]))
            for child in node.get("children") or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(plan)
    return found


async def _assert_range_scans(conn: BaseSurrealConnection, statements: list[tuple[str, dict[str, Any]]]) -> set[type]:
    """``EXPLAIN`` each statement; return the kinds of cursor key seen."""
    assert statements, "the export sent no statement after the first batch"
    kinds: set[type] = set()
    for sql, variables in statements:
        assert sql.endswith(";")
        response = await conn.query(sql[:-1] + " EXPLAIN;", variables)
        assert response.is_ok, response.raw
        operators = _operators(response.results[-1].result)
        assert "RecordIdScan" in operators, (sql, operators)
        assert "TableScan" not in operators, (sql, operators)
        assert not [op for op in operators if "Sort" in op], (sql, operators)
        kinds.add(type(variables["_x_after"].after.id))
    return kinds


# ---------------------------------------------------------------------------
# The plan: a record-id range scan after the first batch
# ---------------------------------------------------------------------------


ALL_KINDS = {int, str, UUID, tuple, frozendict}


@pytest.mark.parametrize("where", [None, "v >= $min"], ids=["no-where", "where"])
async def test_sdk_batches_after_the_first_are_record_id_range_scans(
    tables: None, sent: list[tuple[str, dict[str, Any]]], where: str | None
) -> None:
    conn = await _open()
    try:
        options: dict[str, Any] = {} if where is None else {"where": where, "vars": {"min": 0}}
        rows = [row async for batch in conn.iter_records(MIXED, batch_size=1, **options) for row in batch]
        assert len(rows) == len(KEYS)
        after_first = [(sql, v) for sql, v in sent if "_x_after" in v]
        assert len(after_first) == len(KEYS)  # one per batch after the first, the last one empty
        assert await _assert_range_scans(conn, after_first) == ALL_KINDS
        assert all(sql.startswith("SELECT * FROM $_x_after ") for sql, _ in after_first)
    finally:
        await conn.close()


class I454Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=MIXED)

    id: Any = None
    v: int = 0
    even: bool = False


@pytest.fixture
async def orm(tables: None) -> AsyncGenerator[None, None]:
    SurrealDBConnectionManager.set_connection(URL, "root", "root", NS, DB)
    try:
        yield
    finally:
        await SurrealDBConnectionManager.close_connection()
        await SurrealDBConnectionManager.unset_connection()


async def test_orm_filter_and_prelude_batches_are_record_id_range_scans(
    orm: None, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    inner = I454Item.objects().filter(v__gte=0).select("id")
    qs = I454Item.objects().filter(v__lt=100).filter(id__in=Subquery(inner))

    lines = [line async for line in qs.iter_jsonl(batch_size=1)]

    conn = await _open()
    try:
        after_first = [(sql, v) for sql, v in sent if "_x_after" in v]
        assert await _assert_range_scans(conn, after_first) == ALL_KINDS
        assert all(sql.startswith("LET $_sq0 = ") and " SELECT * FROM $_x_after WHERE " in sql for sql, _ in after_first)
        expected = await _ordered_ids(conn, MIXED)
        assert [_plain(loads_record(line)["id"]) for line in lines] == [_plain(i) for i in expected]
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# The result: every record once, in ORDER BY id order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch_size", [1, 2, 5, 1000])
async def test_every_record_once_in_id_order(conn: BaseSurrealConnection, batch_size: int) -> None:
    ids = [row["id"] async for batch in conn.iter_records(MIXED, batch_size=batch_size) for row in batch]
    assert ids == await _ordered_ids(conn, MIXED)
    assert len(set(map(repr, ids))) == len(KEYS)


@pytest.mark.parametrize("batch_size", [1, 2, 5])
async def test_limit_is_honoured(conn: BaseSurrealConnection, batch_size: int) -> None:
    ids = [row["id"] async for batch in conn.iter_records(MIXED, batch_size=batch_size, limit=7) for row in batch]
    assert ids == (await _ordered_ids(conn, MIXED))[:7]


@pytest.mark.parametrize("batch_size", [1, 2, 1000])
async def test_where_keeps_only_matching_records_in_order(conn: BaseSurrealConnection, batch_size: int) -> None:
    ids = [
        row["id"]
        async for batch in conn.iter_records(MIXED, batch_size=batch_size, where="even = $e", vars={"e": True})
        for row in batch
    ]
    assert ids == await _ordered_ids(conn, MIXED, "WHERE even = true")
    assert len(ids) == len(KEYS) // 2


@pytest.mark.parametrize("batch_size", [1, 2])
async def test_edge_table_is_read_completely(conn: BaseSurrealConnection, batch_size: int) -> None:
    rows = [row async for batch in conn.iter_records(EDGE, batch_size=batch_size) for row in batch]
    assert [row["id"] for row in rows] == await _ordered_ids(conn, EDGE)
    assert len(rows) == 5
    assert all(isinstance(row["in"], RecordId) and isinstance(row["out"], RecordId) for row in rows)


@pytest.mark.parametrize("batch_size", [1, 2, 3])
async def test_a_cursor_deleted_between_batches_loses_nothing(conn: BaseSurrealConnection, batch_size: int) -> None:
    await _fill(conn, DOOMED)
    expected = await _ordered_ids(conn, DOOMED)

    seen: list[RecordId] = []
    deleted: list[RecordId] = []
    async for batch in conn.iter_records(DOOMED, batch_size=batch_size):
        seen.extend(row["id"] for row in batch)
        # The next statement starts after this record, which no longer exists.
        deleted.append(batch[-1]["id"])
        response = await conn.query("DELETE $cursor;", {"cursor": deleted[-1]})
        assert response.is_ok, response.raw

    assert seen == expected
    assert await _ordered_ids(conn, DOOMED) == [i for i in expected if i not in deleted]
