r"""Integration tests for #322 (v2 backport of #318): record references in SDK transactions.

``HTTPTransaction`` used to write each ``table:id`` reference into the batched
SQL as given, so an ID that is not a plain identifier failed to parse or, for
``a-b``, parsed as an expression and wrote nothing. The reference is bound now.

For each ID, passed both as written and in the ``format_thing()`` form, the
test drives every method of the transaction against a record with that ID and
checks that the record is the one that changed and nothing else did: the table
list (``INFO FOR DB``) and every other row are identical before and after.
The same IDs go through ``WebSocketTransaction``, which hands them to RPC
calls as ``RecordId`` values.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm.utils import format_thing
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.protocol.cbor import RecordId
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER, SURREALDB_WS_URL

SURREALDB_DATABASE = "test_issue_322"

NODES = "i322_nodes"
LINKS = "i322_links"
OTHERS = "i322_others"

SPECIAL_IDS = [
    "a b",
    "a`b",
    "dir\\",
    "a-b",
    "p;q",
    "7abc",
    "a\\`b",
    "x$v",
    "x; DELETE i322_nodes; --",
    "x`; DELETE i322_others; --\\",
]

#: Rows the target record must never affect; ``x`` and ``a`` are prefixes of target IDs.
BYSTANDERS = ["keep", "x", "a", "p", "dir", "5", "`-5`"]

#: Integer IDs as written inline, and the ID the server reads from each: a signed
#: 64-bit integer, or — outside that range — the digits as a string ID.
INTEGER_IDS: dict[str, int | str] = {
    "-5": -5,
    "1234567890123456789": 1234567890123456789,
    "9223372036854775807": 2**63 - 1,
    "-9223372036854775808": -(2**63),
    "9223372036854775808": "9223372036854775808",
}

FORMS = {"as_written": lambda raw: f"{NODES}:{raw}", "format_thing": lambda raw: format_thing(NODES, raw)}


async def _ensure_db(conn: HTTPConnection | WebSocketConnection) -> None:
    await conn.query(
        f"DEFINE NAMESPACE IF NOT EXISTS `{SURREALDB_NAMESPACE}`; DEFINE DATABASE IF NOT EXISTS `{SURREALDB_DATABASE}`;"
    )


@pytest.fixture
async def http() -> AsyncGenerator[HTTPConnection, None]:
    conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    await _ensure_db(conn)
    yield conn
    await conn.close()


@pytest.fixture
async def ws() -> AsyncGenerator[WebSocketConnection, None]:
    conn = WebSocketConnection(SURREALDB_WS_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, auto_reconnect=False)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    await _ensure_db(conn)
    yield conn
    await conn.close()


async def _seed(conn: HTTPConnection | WebSocketConnection) -> None:
    await conn.query(
        f"REMOVE TABLE IF EXISTS {NODES}; REMOVE TABLE IF EXISTS {LINKS}; REMOVE TABLE IF EXISTS {OTHERS};"
        f"DEFINE TABLE {LINKS} TYPE RELATION;"
    )
    for name in BYSTANDERS:
        await conn.query(f"CREATE {NODES}:{name} SET v = 0;")
    await conn.query(f"CREATE {OTHERS}:one SET v = 0;")


async def _tables(conn: HTTPConnection | WebSocketConnection) -> set[str]:
    info = (await conn.query("INFO FOR DB;")).first_result
    assert info is not None and info.result is not None
    return set(info.result["tables"])


async def _rows(conn: HTTPConnection | WebSocketConnection) -> list[Any]:
    result = await conn.query(f"SELECT * FROM {NODES}, {OTHERS}, {LINKS};")
    return sorted(result.all_records, key=repr)


async def _record(conn: HTTPConnection | WebSocketConnection, raw: str | int) -> list[Any]:
    result = await conn.query(f"SELECT * FROM type::thing('{NODES}', $id);", {"id": raw})
    return result.all_records


async def _edges(conn: HTTPConnection | WebSocketConnection) -> list[Any]:
    return (await conn.query(f"SELECT in, out, w FROM {LINKS};")).all_records


async def _round_trip(conn: HTTPConnection | WebSocketConnection, raw: str | int, ref: str) -> None:
    """Drive every method against ``ref`` and check only that record changes."""
    await _seed(conn)
    tables = await _tables(conn)
    before = await _rows(conn)
    target = RecordId(table=NODES, id=raw)
    keep = f"{NODES}:keep"

    async with conn.transaction() as tx:
        await tx.create(ref, {"v": 1})
    assert await _record(conn, raw) == [{"id": target, "v": 1}]

    async with conn.transaction() as tx:
        await tx.update(ref, {"v": 2})
    assert await _record(conn, raw) == [{"id": target, "v": 2}]

    async with conn.transaction() as tx:
        await tx.merge(ref, {"m": 3})
    assert await _record(conn, raw) == [{"id": target, "v": 2, "m": 3}]

    async with conn.transaction() as tx:
        await tx.upsert(ref, {"v": 4})
    # HTTP queues ``UPSERT … SET`` (keeps ``m``), the RPC call replaces the content.
    assert [{k: r[k] for k in ("id", "v")} for r in await _record(conn, raw)] == [{"id": target, "v": 4}]

    async with conn.transaction() as tx:
        await tx.relate(ref, LINKS, keep, {"w": 1})
        await tx.relate(keep, LINKS, ref, {"w": 2})
    edges = sorted(await _edges(conn), key=lambda e: e["w"])
    assert edges == [
        {"in": target, "out": RecordId(table=NODES, id="keep"), "w": 1},
        {"in": RecordId(table=NODES, id="keep"), "out": target, "w": 2},
    ]
    await conn.query(f"DELETE {LINKS};")

    async with conn.transaction() as tx:
        await tx.delete(ref)
    assert await _record(conn, raw) == []

    assert await _tables(conn) == tables
    assert await _rows(conn) == before


@pytest.mark.integration
@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("raw", SPECIAL_IDS)
async def test_transaction_record_references_round_trip(http: HTTPConnection, raw: str, form: str) -> None:
    await _round_trip(http, raw, FORMS[form](raw))


@pytest.mark.integration
@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("raw", SPECIAL_IDS)
async def test_websocket_transaction_record_references_round_trip(ws: WebSocketConnection, raw: str, form: str) -> None:
    await _round_trip(ws, raw, FORMS[form](raw))


@pytest.mark.integration
@pytest.mark.parametrize("written", INTEGER_IDS)
async def test_transaction_integer_record_references_round_trip(http: HTTPConnection, written: str) -> None:
    """``t:-5`` and the rest of the i64 range stay integer IDs, as SurrealQL reads them inline."""
    await _round_trip(http, INTEGER_IDS[written], f"{NODES}:{written}")


@pytest.mark.integration
@pytest.mark.parametrize("written", INTEGER_IDS)
async def test_transaction_integer_reference_targets_the_inline_record(http: HTTPConnection, written: str) -> None:
    """A record created by inline SurrealQL is the one the transaction updates and deletes."""
    await _seed(http)
    await http.query(f"CREATE {NODES}:{written} SET v = 1;")
    async with http.transaction() as tx:
        await tx.update(f"{NODES}:{written}", {"v": 2})
    assert await _record(http, INTEGER_IDS[written]) == [{"id": RecordId(table=NODES, id=INTEGER_IDS[written]), "v": 2}]
    async with http.transaction() as tx:
        await tx.delete(f"{NODES}:{written}")
    assert (await http.query(f"SELECT * FROM {NODES}:{written};")).all_records == []


#: The RPC calls ``WebSocketTransaction`` makes convert ``table:id`` with the
#: connection's ``_to_thing()``, which keeps every ID a string — ``t:1`` included,
#: before this change as after it. Only the out-of-range ID, a string on both
#: paths, agrees with ``HTTPTransaction``.
_RPC_STRING_IDS = pytest.mark.xfail(
    strict=True, reason="WebSocketConnection._to_thing() binds integer IDs as strings (connection-level, pre-existing)"
)


@pytest.mark.integration
@pytest.mark.parametrize(
    "written",
    [pytest.param(w, marks=_RPC_STRING_IDS) if isinstance(v, int) else w for w, v in INTEGER_IDS.items()],
)
async def test_websocket_transaction_integer_record_references_round_trip(ws: WebSocketConnection, written: str) -> None:
    await _round_trip(ws, INTEGER_IDS[written], f"{NODES}:{written}")


@pytest.mark.integration
async def test_plain_integer_and_table_references_still_work(http: HTTPConnection) -> None:
    """References written as they are keep their meaning: ``:1`` is the integer ID 1."""
    await _seed(http)
    async with http.transaction() as tx:
        await tx.create(f"{NODES}:1", {"v": 1})
        await tx.create(f"{NODES}:plain_id", {"v": 1})
        await tx.create(NODES, {"v": 9})
    one = await http.query(f"SELECT * FROM {NODES}:1;")
    assert one.all_records == [{"id": RecordId(table=NODES, id=1), "v": 1}]
    plain = await http.query(f"SELECT * FROM {NODES}:plain_id;")
    assert plain.all_records == [{"id": RecordId(table=NODES, id="plain_id"), "v": 1}]
    generated = await http.query(f"SELECT * FROM {NODES} WHERE v = 9;")
    assert len(generated.all_records) == 1
