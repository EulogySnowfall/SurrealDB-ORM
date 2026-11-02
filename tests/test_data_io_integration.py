"""JSON Lines export/import against the pinned server, over HTTP and WebSocket.

I1 (export half): a table holding every value the codec carries, under nine
kinds of record id, exported in batches of 2; each line reads back equal to the
row the server returns. I2: a geometry (decoded as a tuple) is refused, naming
the field. I12 (``iter_records()`` variant): a 6 MB batch over WebSocket, above
aiohttp's former 4 MiB reply cap, arrives as one batch.

I1 (import half): the export loads into another table record for record. I3: a
SCHEMAFULL target rejects a bad batch (E8, earlier batches kept), ``ignore``
skips existing ids, ``raw`` turns off ``VALUE`` and events. I4: edges
round-trip as edges, a mixed file is refused (E7), a ``TYPE NORMAL`` table keeps
``in``/``out`` links as plain fields. I5: batches stay under the byte bound; a
single line over the HTTP RPC limit fails as E8b with the report.

I6 (export half): an ORM model with a ``ForeignKey``, a ``Decimal``, a
``datetime`` and an aliased column; ``filter()`` and ``limit()`` select the
exported rows, which carry the column name, not the alias. I6 (import half):
the export loads into another database through ``using()`` as models equal to
the originals, and drops the cached result of the target table.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.fields.relation import ForeignKey
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.exceptions import ConnectionError, DataFormatError, DataImportError
from surreal_sdk.jsonl import ImportReport, dumps_record, loads_record
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


# ---------------------------------------------------------------------------
# Import (I1 import half, I3, I4, I5)
# ---------------------------------------------------------------------------

COPY = "dio_values_copy"


async def _reset(conn: BaseSurrealConnection, *tables: str) -> None:
    response = await conn.query(" ".join(f"REMOVE TABLE IF EXISTS {t};" for t in tables))
    assert response.is_ok, response.raw


async def _rows(conn: BaseSurrealConnection, table: str) -> list[dict[str, Any]]:
    response = await conn.query(f"SELECT * FROM {table} ORDER BY id;")
    assert response.is_ok, response.raw
    return response.first_result.records


async def _value(conn: BaseSurrealConnection, sql: str) -> Any:
    response = await conn.query(sql)
    assert response.is_ok, response.raw
    return response.results[-1].result


def _as_table(record: Mapping[str, Any], table: str) -> dict[str, Any]:
    """``record`` as it reads back from ``table``: id table rewritten (M21), ``NULL`` gone (``None`` → ``NONE``)."""
    out = {k: v for k, v in record.items() if v is not None}
    out["id"] = RecordId(table, record["id"].id)
    return out


async def test_import_round_trips_the_export_record_for_record(conn: BaseSurrealConnection, tmp_path: Path) -> None:
    dest = tmp_path / "values.jsonl"
    await _reset(conn, COPY)
    try:
        assert await conn.export_jsonl(VALUES, dest, batch_size=2) == 9
        with dest.open("a", encoding="utf-8") as file:
            file.write('{"id":{"$record":["x","hand"]},"dur":{"$duration":"1h30m"}}\n')

        report = await conn.import_jsonl(COPY, dest, batch_size=2)

        assert report == ImportReport(table=COPY, lines=10, rows=10, batches=5)
        source = [_as_table(r, COPY) for r in await _rows(conn, VALUES)]
        copied = [r for r in await _rows(conn, COPY) if r["id"] != RecordId(COPY, "hand")]
        assert [_comparable(r) for r in copied] == [_comparable(r) for r in source]
        assert await _value(conn, f"SELECT VALUE type::of(dur) FROM {COPY};") == ["duration"] * 10
        assert await _value(conn, f"SELECT VALUE <string>dur FROM {COPY}:hand;") == ["1h30m"]
    finally:
        await _reset(conn, COPY)


STRICT = "dio_strict"
STAMPED = "dio_stamped"
STAMP_LOG = "dio_stamped_log"


def _strict_line(i: int, n: Any) -> str:
    return dumps_record({"id": RecordId(STRICT, i), "n": n})


async def test_schemafull_target_rejects_a_bad_batch_and_keeps_the_earlier_ones(conn: BaseSurrealConnection) -> None:
    await _reset(conn, STRICT)
    await conn.query(f"DEFINE TABLE {STRICT} SCHEMAFULL; DEFINE FIELD n ON {STRICT} TYPE int;")
    try:
        lines = [_strict_line(i, i) for i in range(1, 5)] + [_strict_line(5, "str"), _strict_line(6, 6)]

        with pytest.raises(DataImportError) as info:
            await conn.import_jsonl(STRICT, lines, batch_size=2)

        error = info.value
        assert str(error).startswith(f"Batch at lines 5-6 was rejected by {STRICT}: ")
        assert error.report == ImportReport(table=STRICT, lines=4, rows=4, batches=2)
        assert error.code is None and len(error.errors) == 1
        assert [r["n"] for r in await _rows(conn, STRICT)] == [1, 2, 3, 4]

        with pytest.raises(DataImportError, match="already exists"):
            await conn.import_jsonl(STRICT, [_strict_line(1, 1), _strict_line(7, 7)])
        assert len(await _rows(conn, STRICT)) == 4  # the batch is atomic

        report = await conn.import_jsonl(STRICT, [_strict_line(1, 100), _strict_line(7, 7)], on_conflict="ignore")
        assert report == ImportReport(table=STRICT, lines=2, rows=2, batches=1)
        assert [r["n"] for r in await _rows(conn, STRICT)] == [1, 2, 3, 4, 7]
    finally:
        await _reset(conn, STRICT)


@pytest.mark.parametrize("raw", [True, False])
async def test_raw_keeps_value_fields_and_fires_no_event(conn: BaseSurrealConnection, raw: bool) -> None:
    await _reset(conn, STAMPED, STAMP_LOG)
    await conn.query(
        f"DEFINE TABLE {STAMP_LOG}; DEFINE TABLE {STAMPED} SCHEMAFULL; DEFINE FIELD n ON {STAMPED} TYPE int; "
        f"DEFINE FIELD stamp ON {STAMPED} TYPE datetime VALUE time::now(); "
        f"DEFINE EVENT logged ON {STAMPED} WHEN $event = 'CREATE' THEN (CREATE {STAMP_LOG} SET rec = $after.id);"
    )
    old = datetime(2001, 1, 1, tzinfo=UTC)
    try:
        line = dumps_record({"id": RecordId(STAMPED, 1), "n": 1, "stamp": old})

        await conn.import_jsonl(STAMPED, [line], raw=raw)

        stamp = await _value(conn, f"SELECT VALUE <string>stamp FROM {STAMPED}:1;")
        events = await _value(conn, f"SELECT VALUE rec FROM {STAMP_LOG};")
        if raw:
            assert stamp == ["2001-01-01T00:00:00Z"]
            assert events == []
        else:
            assert stamp != ["2001-01-01T00:00:00Z"]
            assert events == [RecordId(STAMPED, 1)]
    finally:
        await _reset(conn, STAMPED, STAMP_LOG)


NODE = "dio_node"
REL = "dio_rel"
REL_COPY = "dio_rel_copy"
ANY_EDGES = "dio_any_edges"
ANY_COPY = "dio_any_copy"
NORMAL = "dio_normal"


@pytest.fixture
async def graph(conn: BaseSurrealConnection) -> AsyncGenerator[BaseSurrealConnection, None]:
    tables = (NODE, REL, REL_COPY, ANY_EDGES, ANY_COPY, NORMAL)
    await _reset(conn, *tables)
    response = await conn.query(
        f"CREATE {NODE}:1; CREATE {NODE}:2; CREATE {NODE}:3; "
        f"DEFINE TABLE {REL} TYPE RELATION IN {NODE} OUT {NODE}; "
        f"RELATE {NODE}:1->{REL}->{NODE}:2; RELATE {NODE}:1->{REL}->{NODE}:3; "
        f"DEFINE TABLE {ANY_EDGES} TYPE ANY; RELATE {NODE}:2->{ANY_EDGES}->{NODE}:3;"
    )
    assert response.is_ok, response.raw
    try:
        yield conn
    finally:
        await _reset(conn, *tables)


async def test_relation_table_round_trips_as_edges(graph: BaseSurrealConnection, tmp_path: Path) -> None:
    dest = tmp_path / "rel.jsonl"
    assert await graph.export_jsonl(REL, dest) == 2
    await graph.query(f"DEFINE TABLE {REL_COPY} TYPE RELATION IN {NODE} OUT {NODE};")

    report = await graph.import_jsonl(REL_COPY, dest)

    assert report.rows == 2
    targets = await _value(graph, f"SELECT VALUE ->{REL_COPY}->{NODE} FROM ONLY {NODE}:1;")
    assert sorted(t.id for t in targets) == [2, 3]


async def test_type_any_edges_round_trip_as_edges(graph: BaseSurrealConnection, tmp_path: Path) -> None:
    dest = tmp_path / "any.jsonl"
    assert await graph.export_jsonl(ANY_EDGES, dest) == 1

    await graph.import_jsonl(ANY_COPY, dest)

    assert await _value(graph, f"SELECT VALUE ->{ANY_COPY}->{NODE} FROM ONLY {NODE}:2;") == [RecordId(NODE, 3)]


async def test_mixed_edges_and_rows_are_refused_e7(graph: BaseSurrealConnection) -> None:
    await graph.query(f"DEFINE TABLE {ANY_COPY} TYPE ANY;")
    lines = [
        dumps_record({"id": RecordId("x", 1), "in": RecordId(NODE, 1), "out": RecordId(NODE, 2)}),
        dumps_record({"id": RecordId("x", 2), "v": 1}),
    ]
    with pytest.raises(DataFormatError, match=r"^Lines 1-2: some rows have record 'in' and 'out' fields"):
        await graph.import_jsonl(ANY_COPY, lines)
    assert await _value(graph, f"SELECT VALUE id FROM {ANY_COPY};") == []


async def test_type_normal_table_keeps_in_and_out_as_plain_fields(graph: BaseSurrealConnection) -> None:
    await graph.query(f"DEFINE TABLE {NORMAL} TYPE NORMAL;")
    lines = [dumps_record({"id": RecordId(NORMAL, i), "in": RecordId(NODE, 1), "out": RecordId(NODE, 2)}) for i in (1, 2)]

    report = await graph.import_jsonl(NORMAL, lines)

    assert report.rows == 2
    rows = await _rows(graph, NORMAL)
    assert [(r["in"], r["out"]) for r in rows] == [(RecordId(NODE, 1), RecordId(NODE, 2))] * 2


BULK = "dio_bulk"


async def test_default_byte_bound_splits_3000_rows_of_1_kb(conn: BaseSurrealConnection) -> None:
    await _reset(conn, BULK)
    logged: list[dict[str, Any]] = []
    lines = (dumps_record({"id": RecordId(BULK, i), "pad": "x" * 1000}) for i in range(3000))
    try:
        report = await conn.import_jsonl(BULK, lines, _on_query=lambda sql, vars, ms: logged.append(vars))

        assert report.rows == 3000
        summaries = [v["_x_rows"] for v in logged if "_x_rows" in v]
        assert len(summaries) == report.batches >= 3
        assert all(isinstance(s, dict) and s["bytes"] <= 1_048_576 for s in summaries)
        assert sum(s["rows"] for s in summaries) == 3000
        assert await _value(conn, f"SELECT count() FROM {BULK} GROUP ALL;") == [{"count": 3000}]
    finally:
        await _reset(conn, BULK)


async def test_a_line_above_the_http_rpc_limit_is_e8b_with_the_report(source_tables: None) -> None:
    conn = await _open("http")
    await _reset(conn, BULK)
    try:
        small = [dumps_record({"id": RecordId(BULK, i), "v": i}) for i in range(3)]
        big = dumps_record({"id": RecordId(BULK, 99), "blob": "x" * 5_000_000})

        with pytest.raises(DataImportError) as info:
            await conn.import_jsonl(BULK, [*small, big])

        error = info.value
        assert str(error).startswith(f"Batch at lines 4-4 could not be sent to {BULK} (ConnectionError: ")
        assert isinstance(error.__cause__, ConnectionError)
        assert error.report == ImportReport(table=BULK, lines=3, rows=3, batches=1)
        assert len(await _rows(conn, BULK)) == 3
    finally:
        await _reset(conn, BULK)
        await conn.close()


# ---------------------------------------------------------------------------
# I6 — ORM export (QuerySet.export_jsonl / iter_jsonl)
# ---------------------------------------------------------------------------


class DIOAuthor(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dio_author")

    id: str | None = None
    name: str = ""


class DIOPost(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dio_post")

    id: str | None = None
    title: str = ""
    published: bool = False
    price: Decimal = Decimal("0")
    author: ForeignKey("DIOAuthor") = None  # type: ignore[valid-type]
    created_at: datetime = Field(alias="created")


@pytest.fixture(scope="module")
async def orm_posts() -> AsyncGenerator[list[DIOPost], None]:
    """Six ``dio_post`` rows saved through the ORM, four of them published, on ``test_data_io``."""
    conn = await _open("http")  # creates the database (M45)
    await conn.close()
    SurrealDBConnectionManager.set_connection(URL, "root", "root", NS, DB)
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS dio_post; REMOVE TABLE IF EXISTS dio_author;")
    try:
        alice = DIOAuthor(id="alice", name="Alice")
        await alice.save()
        posts = []
        for i in range(6):
            post = DIOPost(
                id=f"p{i}",
                title=f"Post {i}",
                published=i not in (1, 4),
                price=Decimal(f"{i}.10"),
                author=alice,
                created_at=datetime(2026, 1, i + 1, 3, 4, 5, 123456, tzinfo=UTC),
            )
            await post.save()
            posts.append(post)
        yield posts
    finally:
        await client.query("REMOVE TABLE IF EXISTS dio_post; REMOVE TABLE IF EXISTS dio_author;")
        await SurrealDBConnectionManager.close_connection()
        await SurrealDBConnectionManager.unset_connection()


async def test_orm_export_writes_only_the_filtered_rows_by_column_name(orm_posts: list[DIOPost], tmp_path: Path) -> None:
    path = tmp_path / "posts.jsonl"

    count = await DIOPost.objects().filter(published=True).export_jsonl(path, batch_size=2)

    records = [loads_record(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert count == 4
    assert [r["id"] for r in records] == [RecordId("dio_post", f"p{i}") for i in (0, 2, 3, 5)]
    for record in records:
        i = int(record["id"].id[1:])
        assert record["published"] is True
        assert record["author"] == RecordId("dio_author", "alice")
        assert record["price"] == Decimal(f"{i}.10")
        assert record["created"] == datetime(2026, 1, i + 1, 3, 4, 5, 123456, tzinfo=UTC)
        assert "created_at" not in record


async def test_orm_iter_jsonl_honours_limit(orm_posts: list[DIOPost]) -> None:
    lines = [line async for line in DIOPost.objects().limit(3).iter_jsonl(batch_size=2)]

    assert [loads_record(line)["id"] for line in lines] == [RecordId("dio_post", f"p{i}") for i in range(3)]


# ---------------------------------------------------------------------------
# I6 — ORM import (QuerySet.import_jsonl)
# ---------------------------------------------------------------------------

DST_DB = "test_data_io_dst"
DST = "dio_dst"


async def test_orm_import_loads_the_export_into_another_database(orm_posts: list[DIOPost], tmp_path: Path) -> None:
    path = tmp_path / "posts.jsonl"
    await DIOPost.objects().filter(published=True).export_jsonl(path, batch_size=2)
    originals = await DIOPost.objects().filter(published=True).order_by("id").exec()
    assert len(originals) == 4

    SurrealDBConnectionManager.add_connection(DST, url=URL, user="root", password="root", namespace=NS, database=DST_DB)
    try:
        dst = await SurrealDBConnectionManager.get_client(DST)  # the ORM's connection creates the database
        await dst.query("REMOVE TABLE IF EXISTS dio_post; DEFINE TABLE dio_post SCHEMALESS;")
        async with SurrealDBConnectionManager.using(DST):
            assert await DIOPost.objects().cache(ttl=60).exec() == []  # cached: an empty table

            report = await DIOPost.objects().import_jsonl(path, batch_size=3)

            cached = await DIOPost.objects().cache(ttl=60).exec()  # the import dropped the cached []
            loaded = await DIOPost.objects().order_by("id").exec()
        assert report == ImportReport(table="dio_post", lines=4, rows=4, batches=2)
        assert len(cached) == 4
        assert [p.model_dump() for p in loaded] == [p.model_dump() for p in originals]
        for post in loaded:
            assert isinstance(post.price, Decimal)
            assert post.created_at.tzinfo is not None
    finally:
        try:
            await (await SurrealDBConnectionManager.get_client(DST)).query("REMOVE TABLE IF EXISTS dio_post;")
        finally:
            await SurrealDBConnectionManager.remove_connection(DST)
