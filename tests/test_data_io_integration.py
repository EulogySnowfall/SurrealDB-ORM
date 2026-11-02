"""JSON Lines export/import against the pinned server, over HTTP and WebSocket.

I1 (export half): a table holding every value the codec carries, under nine
kinds of record id, exported in batches of 2; each line reads back equal to the
row the server returns. I2: a geometry (decoded as a tuple) is refused, naming
the field. I12 (``iter_records()`` variant): a 6 MB batch over WebSocket, above
aiohttp's former 4 MiB reply cap, arrives as one batch.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import Any

import pytest

from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.exceptions import DataFormatError
from surreal_sdk.jsonl import loads_record
from surreal_sdk.protocol.cbor import RecordId

pytestmark = pytest.mark.integration

URL = "http://localhost:8000"
NS = "test"
DB = "test_data_io"
VALUES = "dio_values"
GEO = "dio_geo"
BIG = "dio_big"

# One row per kind of record id (M22), each holding the M20 value set.
IDS = [
    "alice",
    "bob",
    "42",
    "-7",
    "⟨7 x⟩",
    "u'0190f3b2-7c1e-7a4b-9f00-123456789abc'",
    "[1, 'a']",
    "{ k: 1 }",
    "{ k: 2 }",
]
FIELDS = (
    "s = 'café\\nline', n = 9223372036854775807, f = 1.5, pinf = math::inf, ninf = -math::inf, nan = NaN, "
    "dec = 1.10dec, dt = d'2020-01-02T03:04:05.123456789Z', dur = 1h30m, uid = u'0190f3b2-7c1e-7a4b-9f00-123456789abc', "
    "raw = <bytes>'hi', link = person:alice, tbl = type::table('x'), nothing = NULL, flag = true, "
    "nested = { arr: [1, 'two', { '$weird': 1 }], deep: { d: d'2021-06-01T00:00:00Z' } }"
)


async def _open(kind: str, **ws_kwargs: Any) -> BaseSurrealConnection:
    """A signed-in CBOR connection on ``test_data_io``, the database created first (M45)."""
    connection: BaseSurrealConnection
    if kind == "http":
        connection = HTTPConnection(URL, NS, DB)
    else:
        connection = WebSocketConnection(URL, NS, DB, timeout=10, auto_reconnect=False, **ws_kwargs)
    await connection.connect()
    await connection.signin("root", "root")
    await connection.query(f"DEFINE NAMESPACE IF NOT EXISTS {NS}; DEFINE DATABASE IF NOT EXISTS {DB};")
    await connection.use(NS, DB)
    return connection


@pytest.fixture(scope="module")
async def source_tables() -> AsyncGenerator[None, None]:
    """``dio_values`` (9 rows, every id kind), ``dio_geo`` (a point) and ``dio_big`` (6 rows of 1 MB)."""
    conn = await _open("http")
    try:
        await conn.query(f"REMOVE TABLE IF EXISTS {VALUES}; REMOVE TABLE IF EXISTS {GEO}; REMOVE TABLE IF EXISTS {BIG};")
        for key in IDS:
            response = await conn.query(f"CREATE {VALUES}:{key} SET {FIELDS};")
            assert response.is_ok, response.raw
        await conn.query(
            f"DEFINE TABLE {GEO} SCHEMAFULL; DEFINE FIELD loc ON {GEO} TYPE geometry<point>; "
            f"CREATE {GEO}:1 SET loc = (1.5, 2.5);"
        )
        for i in range(6):
            await conn.query(f"CREATE {BIG}:{i} SET blob = string::repeat('x', 1000000);")
        yield
        await conn.query(f"REMOVE TABLE IF EXISTS {VALUES}; REMOVE TABLE IF EXISTS {GEO}; REMOVE TABLE IF EXISTS {BIG};")
    finally:
        await conn.close()


@pytest.fixture(params=["http", "ws"])
async def conn(request: pytest.FixtureRequest, source_tables: None) -> AsyncGenerator[BaseSurrealConnection, None]:
    connection = await _open(request.param)
    try:
        yield connection
    finally:
        await connection.close()


def _comparable(value: Any) -> Any:
    """``value`` with NaN replaced by a marker, so records compare with ``==``.

    A record id's array key is decoded from the server as a ``tuple`` and from
    a line as a ``list``, an object key as a ``frozendict`` and as a ``dict``
    (spec §3.1): both bind back to the same key, so both are compared as lists
    and dicts.
    """
    if isinstance(value, float) and math.isnan(value):
        return "<nan>"
    if isinstance(value, RecordId):
        return ("record", value.table, _comparable(value.id))
    if isinstance(value, Mapping):
        return {k: _comparable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_comparable(v) for v in value]
    return value


async def test_export_writes_every_row_in_id_order_losslessly(conn: BaseSurrealConnection, tmp_path: Path) -> None:
    dest = tmp_path / "values.jsonl"

    count = await conn.export_jsonl(VALUES, dest, batch_size=2)

    expected = (await conn.query(f"SELECT * FROM {VALUES} ORDER BY id;")).first_result.records
    lines = dest.read_text(encoding="utf-8").splitlines()
    assert count == 9
    assert len(lines) == 9
    records = [loads_record(line) for line in lines]
    assert [_comparable(r["id"]) for r in records] == [_comparable(r["id"]) for r in expected]
    assert [_comparable(r) for r in records] == [_comparable(r) for r in expected]


async def test_iter_records_pages_in_batches_of_two(conn: BaseSurrealConnection) -> None:
    sizes = [len(batch) async for batch in conn.iter_records(VALUES, batch_size=2)]
    assert sizes == [2, 2, 2, 2, 1]


async def test_export_where_and_limit(conn: BaseSurrealConnection, tmp_path: Path) -> None:
    dest = tmp_path / "some.jsonl"
    count = await conn.export_jsonl(VALUES, dest, batch_size=2, where="n = $n", vars={"n": 9223372036854775807}, limit=3)
    assert count == 3
    assert len(dest.read_text(encoding="utf-8").splitlines()) == 3


async def test_geometry_is_refused_naming_the_field(conn: BaseSurrealConnection, tmp_path: Path) -> None:
    with pytest.raises(DataFormatError, match=r"^Cannot write loc of dio_geo:1 as JSON Lines: a tuple") as info:
        await conn.export_jsonl(GEO, tmp_path / "geo.jsonl")
    assert info.value.path == "loc"


async def test_ws_iter_records_receives_a_batch_above_4_mib(source_tables: None) -> None:
    conn = await _open("ws")
    try:
        started = time.monotonic()
        batches = [batch async for batch in conn.iter_records(BIG, batch_size=1000)]
        elapsed = time.monotonic() - started
    finally:
        await conn.close()

    assert [len(batch) for batch in batches] == [6]
    assert all(len(row["blob"]) == 1_000_000 for row in batches[0])
    assert elapsed < 5
