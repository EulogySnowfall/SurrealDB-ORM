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

I10: ``SurrealDBConnectionManager.dump_database(tables=[DIOPost])`` writes a
``0o600`` file that ``restore_database()`` loads into another database under
``using()``, dropping the cached result there.

I9: ``export_database()`` sends the requested sections only (``tables=``,
``records=False``, ``users=False``). I11: a database ``EDITOR`` can export and
cannot import (E12b 403).

I7: a dump restored into an empty database gives the same schema and rows,
compared as the server writes them in JSON (ns datetimes, geometry, ``NULL``).
I8: restoring again fails on the existing rows (E12a, 422 on 3.3 and 200 on
3.2); a body without ``OPTION IMPORT;`` is E12b 400 when small and E12c when
large (the server closes before reading it); existing definitions are replaced
and target-only ones kept; an existing row fails its own ``INSERT`` only.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncGenerator, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import Field

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.cache import QueryCache
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
        assert error.report == ImportReport(table=STRICT, lines=6, rows=4, batches=2)
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
        assert error.report == ImportReport(table=BULK, lines=4, rows=3, batches=1)
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


# ---------------------------------------------------------------------------
# I10 — ORM dump/restore (SurrealDBConnectionManager.dump_database / restore_database)
# ---------------------------------------------------------------------------


async def test_orm_dump_and_restore_into_another_database_clears_the_cache(orm_posts: list[DIOPost], tmp_path: Path) -> None:
    path = tmp_path / "posts.surql"
    written = await SurrealDBConnectionManager.dump_database(path, tables=[DIOPost], users=False, accesses=False)
    assert written == path.stat().st_size > 0
    assert path.stat().st_mode & 0o777 == 0o600
    dump = path.read_text(encoding="utf-8")
    assert "DEFINE TABLE dio_post " in dump and "DEFINE TABLE dio_author " not in dump  # model class → its table

    SurrealDBConnectionManager.add_connection(DST, url=URL, user="root", password="root", namespace=NS, database=DST_DB)
    try:
        dst = await SurrealDBConnectionManager.get_client(DST)  # the ORM's connection creates the database
        # Defined and empty, as after migrations: the dump's definition replaces it (D24).
        await dst.query("REMOVE TABLE IF EXISTS dio_post; DEFINE TABLE dio_post SCHEMALESS;")
        QueryCache.clear()  # the I6 import test cached this query on DST; a raw REMOVE does not invalidate it
        async with SurrealDBConnectionManager.using(DST):
            assert await DIOPost.objects().cache(ttl=60).exec() == []  # cached: an empty table

            await SurrealDBConnectionManager.restore_database(path)

            cached = await DIOPost.objects().cache(ttl=60).exec()  # the restore dropped the cached []
            restored = await DIOPost.objects().order_by("id").exec()
        originals = await DIOPost.objects().order_by("id").exec()
        assert len(cached) == len(originals) == 6
        assert [p.model_dump() for p in restored] == [p.model_dump() for p in originals]
    finally:
        try:
            await (await SurrealDBConnectionManager.get_client(DST)).query("REMOVE TABLE IF EXISTS dio_post;")
        finally:
            await SurrealDBConnectionManager.remove_connection(DST)


# ---------------------------------------------------------------------------
# I9 / I11 — database dump (HTTPConnection.export_database*)
# ---------------------------------------------------------------------------

DUMP_A = "dio_dump_a"
DUMP_B = "dio_dump_b"
DUMP_USER = "dio_dump_editor"
DUMP_PASS = "dio-dump-pw"


@pytest.fixture
async def dump_source() -> AsyncGenerator[HTTPConnection, None]:
    """Two small tables, a database ``EDITOR`` user and a param in ``test_data_io``."""
    conn = await _open("http")
    assert isinstance(conn, HTTPConnection)
    cleanup = (
        f"REMOVE TABLE IF EXISTS {DUMP_A}; REMOVE TABLE IF EXISTS {DUMP_B}; "
        f"REMOVE USER IF EXISTS {DUMP_USER} ON DATABASE; REMOVE PARAM IF EXISTS $dio_dump_p;"
    )
    try:
        await conn.query(cleanup)
        response = await conn.query(
            f"CREATE {DUMP_A}:1 SET v = 'dio-row-a'; CREATE {DUMP_B}:1 SET v = 'dio-row-b'; "
            f"DEFINE USER {DUMP_USER} ON DATABASE PASSWORD '{DUMP_PASS}' ROLES EDITOR; "
            "DEFINE PARAM $dio_dump_p VALUE 1;"
        )
        assert response.is_ok, response.raw
        yield conn
        await conn.query(cleanup)
    finally:
        await conn.close()


async def _dump(conn: HTTPConnection, **options: Any) -> str:
    async with conn.export_database_stream(**options) as chunks:
        return b"".join([chunk async for chunk in chunks]).decode("utf-8")


async def test_export_database_honours_tables_records_and_users(dump_source: HTTPConnection, tmp_path: Path) -> None:
    path = tmp_path / "db.surql"
    written = await dump_source.export_database(path, tables=[DUMP_A])
    full = path.read_text(encoding="utf-8")
    assert written == len(path.read_bytes())
    statements = [line for line in full.splitlines() if line.strip() and not line.startswith("--")]
    assert statements[0] == "OPTION IMPORT;"
    assert f"DEFINE TABLE {DUMP_A} " in full and "dio-row-a" in full
    assert f"DEFINE TABLE {DUMP_B} " not in full and "dio-row-b" not in full
    assert f"DEFINE USER {DUMP_USER} ON DATABASE" in full
    assert "DEFINE PARAM $dio_dump_p" in full

    no_rows = await _dump(dump_source, tables=[DUMP_A, DUMP_B], records=False)
    assert f"DEFINE TABLE {DUMP_A} " in no_rows and f"DEFINE TABLE {DUMP_B} " in no_rows
    assert "INSERT" not in no_rows and "dio-row-a" not in no_rows

    no_users = await _dump(dump_source, tables=[DUMP_A], users=False, params=False)
    assert "DEFINE USER" not in no_users and "DEFINE PARAM" not in no_users
    assert f"DEFINE TABLE {DUMP_A} " in no_users and "dio-row-a" in no_users


async def test_a_database_editor_can_export(dump_source: HTTPConnection) -> None:
    editor = HTTPConnection(URL, NS, DB)
    await editor.connect()
    try:
        await editor.signin(DUMP_USER, DUMP_PASS, namespace=NS, database=DB)
        text = await _dump(editor, tables=[DUMP_B])
    finally:
        await editor.close()
    assert f"DEFINE TABLE {DUMP_B} " in text and "dio-row-b" in text


async def test_a_database_editor_cannot_import_e12b_403(dump_source: HTTPConnection) -> None:
    editor = HTTPConnection(URL, NS, DB)
    await editor.connect()
    try:
        await editor.signin(DUMP_USER, DUMP_PASS, namespace=NS, database=DB)
        with pytest.raises(DataImportError) as caught:
            await editor.import_database(f"OPTION IMPORT;\nCREATE {DUMP_A}:9 SET v = 'editor';\n".encode())
    finally:
        await editor.close()

    assert caught.value.code == 403
    assert str(caught.value).startswith("Import failed: HTTP 403 - ")
    assert "Not enough permissions" in caught.value.errors[0]
    rows = (await dump_source.query(f"SELECT * FROM {DUMP_A}:9;")).first_result.records
    assert rows == []


# ---------------------------------------------------------------------------
# I7 / I8 — database restore (HTTPConnection.import_database)
# ---------------------------------------------------------------------------

SRC_DB = "test_data_io_src"
RESTORE_DB = "test_data_io_restore"
R_PEOPLE = "dio_r"
R_PLACES = "dio_s"
R_EDGES = "dio_e"
RESTORED = (R_PEOPLE, R_PLACES, R_EDGES)
SOURCE_SCHEMA = (
    f"DEFINE TABLE {R_PEOPLE} SCHEMAFULL PERMISSIONS NONE; "
    f"DEFINE FIELD name ON {R_PEOPLE} TYPE string; "
    f"DEFINE FIELD at ON {R_PEOPLE} TYPE datetime; "
    f"DEFINE INDEX {R_PEOPLE}_name ON {R_PEOPLE} FIELDS name UNIQUE; "
    f"DEFINE TABLE {R_EDGES} TYPE RELATION IN {R_PEOPLE} OUT {R_PLACES} SCHEMALESS; "
    "DEFINE FUNCTION fn::dio_f($x: int) { RETURN $x + 1; }; "
    "DEFINE PARAM $dio_p VALUE 7; "
    f"CREATE {R_PEOPLE}:1 SET name = 'ann', at = d'2020-01-02T03:04:05.123456789Z'; "
    f"CREATE {R_PEOPLE}:2 SET name = 'bob', at = d'2021-06-01T00:00:00Z'; "
    f"CREATE {R_PLACES}:1 SET loc = (1.5, 2.5), gone = NULL, n = 1.10dec; "
    f"CREATE {R_PLACES}:⟨7 x⟩ SET loc = (-73.98, 40.74), tags = ['a', 'b']; "
    f"RELATE {R_PEOPLE}:1->{R_EDGES}->{R_PLACES}:1 SET w = 1; "
    f"RELATE {R_PEOPLE}:2->{R_EDGES}->{R_PLACES}:⟨7 x⟩ SET w = 2;"
)


async def _database(name: str) -> HTTPConnection:
    """A root ``HTTPConnection`` on a new, empty database ``name`` (created first: ``/import`` needs it, M45)."""
    admin = await _open("http")
    try:
        response = await admin.query(f"REMOVE DATABASE IF EXISTS {name}; DEFINE DATABASE {name};")
        assert response.is_ok, response.raw
    finally:
        await admin.close()
    conn = HTTPConnection(URL, NS, name)
    await conn.connect()
    await conn.signin("root", "root")
    return conn


async def _drop_database(name: str) -> None:
    admin = await _open("http")
    try:
        await admin.query(f"REMOVE DATABASE IF EXISTS {name};")
    finally:
        await admin.close()


async def _texts(conn: HTTPConnection, sql: str) -> list[Any]:
    """The results of ``sql`` as the server writes them in JSON (``/sql``): ns datetimes, geometry and ``NULL`` kept."""
    statements = await conn.sql(sql)
    assert all(statement["status"] == "OK" for statement in statements), statements
    return [statement["result"] for statement in statements]


def _schema_sql() -> str:
    return "INFO FOR DB; " + " ".join(f"INFO FOR TABLE {table};" for table in RESTORED)


def _rows_sql(*tables: str) -> str:
    return " ".join(f"SELECT * FROM {table} ORDER BY id;" for table in tables)


@pytest.fixture(scope="module")
async def dump_file(tmp_path_factory: pytest.TempPathFactory) -> AsyncGenerator[tuple[Path, list[Any], list[Any]], None]:
    """The dump of ``test_data_io_src`` (two tables, an edge table, an index, a function, a param), its schema and rows."""
    source = await _database(SRC_DB)
    try:
        response = await source.query(SOURCE_SCHEMA)
        assert response.is_ok, response.raw
        path = tmp_path_factory.mktemp("restore") / "src.surql"
        await source.export_database(path)
        schema = await _texts(source, _schema_sql())
        rows = await _texts(source, _rows_sql(*RESTORED))
    finally:
        await source.close()
    try:
        yield path, schema, rows
    finally:
        await _drop_database(SRC_DB)
        await _drop_database(RESTORE_DB)


@pytest.fixture
async def target() -> AsyncGenerator[HTTPConnection, None]:
    conn = await _database(RESTORE_DB)
    try:
        yield conn
    finally:
        await conn.close()


async def test_restore_into_an_empty_database_reproduces_schema_and_rows(
    dump_file: tuple[Path, list[Any], list[Any]], target: HTTPConnection
) -> None:
    path, schema, rows = dump_file
    assert rows[0][0]["at"] == "2020-01-02T03:04:05.123456789Z"  # the ns digits are compared, not their decoded form
    assert rows[1][0]["gone"] is None and "gone" in rows[1][0]  # NULL, not NONE

    await target.import_database(path)

    assert await _texts(target, _schema_sql()) == schema
    assert await _texts(target, _rows_sql(*RESTORED)) == rows
    assert await _texts(target, "RETURN fn::dio_f(1); RETURN $dio_p;") == [2, 7]


async def test_restoring_the_same_dump_again_is_e12a(
    dump_file: tuple[Path, list[Any], list[Any]], target: HTTPConnection
) -> None:
    path, _, rows = dump_file
    await target.import_database(path)

    with pytest.raises(DataImportError) as caught:
        await target.import_database(path.read_bytes())

    error = caught.value
    assert error.code in (200, 422)  # 422 on 3.3, 200 on 3.2 (M10)
    assert str(error).startswith(f"Import failed: {len(error.errors)} statement(s) rejected; first: ")
    assert all("already exists" in message for message in error.errors), error.errors
    assert await _texts(target, _rows_sql(*RESTORED)) == rows


async def test_a_small_body_without_option_import_is_e12b_400(target: HTTPConnection) -> None:
    with pytest.raises(DataImportError) as caught:
        await target.import_database(f"CREATE {R_PEOPLE}:9 SET name = 'x';\n".encode())

    assert caught.value.code == 400
    assert "Import requires `OPTION IMPORT;` as the first statement" in str(caught.value)
    assert caught.value.errors == [str(caught.value).removeprefix("Import failed: HTTP 400 - ")]


async def test_a_large_body_rejected_up_front_is_e12c(target: HTTPConnection) -> None:
    # M46: the server answers 400 after the first statement and closes without reading the rest;
    # with more left to send than the socket buffers hold, httpx sees the closed connection instead
    # of the response. 16 MiB: at 4 MiB the buffers can still take the whole body now and then.
    statement = f"CREATE {R_PEOPLE}:9 SET name = 'x';\n".encode()
    body = statement * (16 * 1024 * 1024 // len(statement) + 1)

    with pytest.raises(DataImportError) as caught:
        await target.import_database(body)

    assert caught.value.code is None
    assert isinstance(caught.value.__cause__, httpx.TransportError)
    assert str(caught.value).startswith("Import failed: the connection was closed during the upload (")
    assert await _texts(target, "RETURN (INFO FOR DB).tables;") == [{}]  # nothing was run


async def test_restore_replaces_existing_definitions_and_keeps_target_only_ones(
    dump_file: tuple[Path, list[Any], list[Any]], target: HTTPConnection
) -> None:
    # A target prepared by migrations / define_table(): the dumped table exists with another
    # definition and an extra field, and holds no row (M43).
    path, schema, rows = dump_file
    await _texts(
        target,
        f"DEFINE TABLE {R_PEOPLE} SCHEMALESS PERMISSIONS FULL; DEFINE FIELD extra ON {R_PEOPLE} TYPE option<string>;",
    )

    await target.import_database(path)

    info_db, info_people = (await _texts(target, f"INFO FOR DB; INFO FOR TABLE {R_PEOPLE};"))[:2]
    assert info_db["tables"][R_PEOPLE] == schema[0]["tables"][R_PEOPLE]
    assert "extra" in info_people["fields"]
    assert {k: v for k, v in info_people["fields"].items() if k != "extra"} == schema[1]["fields"]
    assert await _texts(target, _rows_sql(*RESTORED)) == rows


async def test_an_existing_row_fails_its_insert_only_e12a(
    dump_file: tuple[Path, list[Any], list[Any]], target: HTTPConnection
) -> None:
    path, _, rows = dump_file
    await _texts(target, f"CREATE {R_PEOPLE}:1 SET name = 'kept', at = d'2000-01-01T00:00:00Z';")

    with pytest.raises(DataImportError) as caught:
        await target.import_database(path)

    error = caught.value
    assert error.code in (200, 422)
    assert error.errors == [f"Database record `{R_PEOPLE}:1` already exists"]
    people, places, edges = await _texts(target, _rows_sql(*RESTORED))
    assert [(row["id"], row["name"]) for row in people] == [(f"{R_PEOPLE}:1", "kept")]  # its INSERT failed whole
    assert places == rows[1]
    assert edges == rows[2]
