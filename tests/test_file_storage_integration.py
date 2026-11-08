"""Integration tests for file storage (0.39.0).

File storage is an experimental SurrealDB capability on 3.2 and 3.3: the server
must be started with ``--allow-experimental=files``. The module first probes
it (``DEFINE BUCKET`` then ``REMOVE BUCKET`` in one request): on a flagless
server every test here is skipped, unless ``SORM_REQUIRE_FILES=1`` (CI, the
SurrealDB monitors) makes that a failure.

The tests share the suite's ``test``/``test`` database, where a leftover bucket
would show up in every later introspection test. So the probe removes its
bucket in the same request, the bucket fixture removes the ``fsi_*`` buckets in
a ``finally``, and its teardown fails if any of them is still defined.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field, ValidationError

from surreal_orm import (
    BaseSurrealModel,
    FileField,
    ImageField,
    LiveAction,
    Q,
    SurrealConfigDict,
    SurrealDBConnectionManager,
)
from surreal_orm.debug import QueryLogger
from surreal_orm.introspection import schema_diff
from surreal_orm.migrations.db_introspector import DatabaseIntrospector
from surreal_orm.migrations.define_parser import parse_define_field
from surreal_orm.migrations.introspector import ModelIntrospector
from surreal_orm.migrations.operations import AddField, AlterBucket, DefineBucket, RemoveBucket
from surreal_orm.migrations.state import BucketState, SchemaState, TableState
from surreal_orm.utils import escape_record_id
from surreal_sdk import FileInfo, FileRef, HTTPConnection, PayloadTooLargeError, WebSocketConnection
from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.exceptions import QueryError
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.types import QueryResponse
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER
from tests.files_capability import FILES_SKIP_REASON, files_capability_outcome, files_required

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = os.getenv("SURREALDB_DATABASE", "test")

PROBE_BUCKET = "sorm_files_probe"
PROBE_SQL = f"DEFINE BUCKET IF NOT EXISTS {PROBE_BUCKET} BACKEND 'memory'; REMOVE BUCKET IF EXISTS {PROBE_BUCKET};"

# The buckets the tests of this module use, with their definition clauses.
FSI_BUCKETS: dict[str, str] = {
    "fsi_files": "BACKEND 'memory'",
    "fsi_ro": "READONLY BACKEND 'memory'",
    "fsi_perm": "BACKEND 'memory' PERMISSIONS WHERE $action INSIDE ['get', 'head', 'exists', 'list']",
}


def _statement_error(response: QueryResponse) -> str | None:
    """The first statement error of a response, or ``None`` when every statement succeeded."""
    for result in response.results:
        if result.is_error:
            return str(result.result)
    return None


async def _probe_files_capability(conn: HTTPConnection) -> str | None:
    """Run the capability probe; return the server's error text, or ``None`` when it succeeded.

    Without the flag, ``DEFINE BUCKET`` is a parse error, which comes back as an
    RPC error (raised by ``query()``); a ``file::*`` failure would be a statement
    error inside a successful RPC. Both are read.
    """
    try:
        response = await conn.query(PROBE_SQL)
    except QueryError as exc:
        return exc.message
    return _statement_error(response)


async def _defined_buckets(conn: HTTPConnection) -> set[str]:
    """The names of the buckets defined in the test database."""
    response = await conn.query("INFO FOR DB;")
    error = _statement_error(response)
    if error is not None:
        raise QueryError(error, query="INFO FOR DB;")
    info = response.first_result.result if response.first_result else None
    buckets = info.get("buckets", {}) if isinstance(info, dict) else {}
    return set(buckets)


def _ours(buckets: set[str]) -> set[str]:
    """The buckets this module could have left behind."""
    return {name for name in buckets if name.startswith("fsi_") or name == PROBE_BUCKET}


@pytest.fixture(scope="module")
async def files_conn() -> AsyncGenerator[HTTPConnection, None]:
    """A root HTTP connection to a server whose files capability was probed."""
    conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    try:
        await conn.signin(SURREALDB_USER, SURREALDB_PASS)
        await conn.query(
            f"DEFINE NAMESPACE IF NOT EXISTS `{SURREALDB_NAMESPACE}`; DEFINE DATABASE IF NOT EXISTS `{SURREALDB_DATABASE}`;"
        )
        error_text = await _probe_files_capability(conn)
        outcome = files_capability_outcome(error_text, files_required())
        if outcome == "skip":
            pytest.skip(FILES_SKIP_REASON)
        if outcome == "fail":
            pytest.fail(f"{FILES_SKIP_REASON}, and SORM_REQUIRE_FILES=1 requires the file tests to run: {error_text}")
        yield conn
    finally:
        await conn.close()


@pytest.fixture(scope="module")
async def fsi_buckets(files_conn: HTTPConnection) -> AsyncGenerator[tuple[str, ...], None]:
    """Define the module's ``fsi_*`` buckets; remove them afterwards and check none is left."""
    names = tuple(FSI_BUCKETS)
    try:
        sql = " ".join(f"DEFINE BUCKET OVERWRITE {name} {clauses};" for name, clauses in FSI_BUCKETS.items())
        error = _statement_error(await files_conn.query(sql))
        if error is not None:
            raise QueryError(error, query=sql)
        yield names
    finally:
        await files_conn.query(" ".join(f"REMOVE BUCKET IF EXISTS {name};" for name in names))
        left = _ours(await _defined_buckets(files_conn))
        if left:
            pytest.fail(f"file storage tests left buckets in {SURREALDB_NAMESPACE}/{SURREALDB_DATABASE}: {sorted(left)}")


async def test_files_capability_probe(files_conn: HTTPConnection, fsi_buckets: tuple[str, ...]) -> None:
    """The probe left no bucket, the module's buckets exist, and a file call runs."""
    defined = await _defined_buckets(files_conn)
    assert PROBE_BUCKET not in defined
    assert set(fsi_buckets) <= defined

    response = await files_conn.query("RETURN file::exists($f);", {"f": FileRef("fsi_files", "/smoke.txt")})
    assert _statement_error(response) is None
    assert response.first_result is not None
    assert response.first_result.result is False


# ---------------------------------------------------------------------------
# I1-I3, I9 - SDK file methods
# ---------------------------------------------------------------------------

ALL_BYTES = bytes(range(256))


@pytest.fixture(params=["http", "ws"])
async def conn(
    request: pytest.FixtureRequest, files_conn: HTTPConnection, fsi_buckets: tuple[str, ...]
) -> AsyncGenerator[BaseSurrealConnection, None]:
    """A root connection over HTTP (the module's) or over a fresh WebSocket."""
    if request.param == "http":
        yield files_conn
        return
    ws = WebSocketConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, auto_reconnect=False)
    await ws.connect()
    try:
        await ws.signin(SURREALDB_USER, SURREALDB_PASS)
        yield ws
    finally:
        await ws.close()


def _area() -> str:
    """A key prefix of its own for one test, so runs and transports never share keys."""
    return f"/t{uuid.uuid4().hex}"


async def test_file_round_trip(conn: BaseSurrealConnection) -> None:
    """I1: put, get, head, exists, copy, rename, delete over HTTP and WebSocket."""
    area = _area()
    ref = FileRef("fsi_files", f"{area}/data.bin")

    assert await conn.file_put(ref, ALL_BYTES) is None
    assert await conn.file_get(ref) == ALL_BYTES
    assert await conn.file_exists(ref) is True

    info = await conn.file_head(ref)
    assert isinstance(info, FileInfo)
    assert info.file == ref
    assert info.size == 256
    assert isinstance(info.updated, datetime)

    copy = await conn.file_copy(ref, f"{area}/copy.bin")
    assert copy == FileRef("fsi_files", f"{area}/copy.bin")
    assert await conn.file_get(copy) == ALL_BYTES

    moved = await conn.file_rename(copy, f"{area[1:]}/moved.bin")  # no leading "/": added
    assert moved == FileRef("fsi_files", f"{area}/moved.bin")
    assert await conn.file_exists(copy) is False
    assert await conn.file_get(moved) == ALL_BYTES

    assert await conn.file_delete(moved) is None
    assert await conn.file_exists(moved) is False
    assert await conn.file_delete(ref) is None


async def test_missing_file(conn: BaseSurrealConnection) -> None:
    """I1: a missing file reads as None, and deleting it succeeds."""
    ref = FileRef("fsi_files", f"{_area()}/missing.bin")
    assert await conn.file_get(ref) is None
    assert await conn.file_head(ref) is None
    assert await conn.file_exists(ref) is False
    assert await conn.file_delete(ref) is None


async def test_overwrite_false_keeps_the_existing_file(conn: BaseSurrealConnection) -> None:
    """I1: the ``*_if_not_exists`` forms keep an existing target, silently."""
    area = _area()
    first = FileRef("fsi_files", f"{area}/first")
    second = FileRef("fsi_files", f"{area}/second")
    await conn.file_put(first, b"one")
    await conn.file_put(second, b"two")

    await conn.file_put(first, b"changed", overwrite=False)
    assert await conn.file_get(first) == b"one"

    assert await conn.file_copy(second, first.key, overwrite=False) == first
    assert await conn.file_get(first) == b"one"

    assert await conn.file_rename(second, first.key, overwrite=False) == first
    assert await conn.file_get(first) == b"one"
    assert await conn.file_get(second) == b"two"

    await conn.file_put(first, b"three", overwrite=False)
    await conn.file_delete(first)
    await conn.file_put(first, b"three", overwrite=False)
    assert await conn.file_get(first) == b"three"
    await conn.file_delete(first)
    await conn.file_delete(second)


async def test_file_list(conn: BaseSurrealConnection) -> None:
    """I2: prefix (with or without "/"), exclusive start, limit, key order."""
    area = _area()
    keys = [f"{area}/p/{n}" for n in (3, 1, 5, 2, 4)]
    for key in keys:
        await conn.file_put(FileRef("fsi_files", key), key.encode())
    try:
        listed = await conn.file_list("fsi_files", prefix=f"{area}/p/")
        assert [info.key for info in listed] == sorted(keys)
        assert all(info.bucket == "fsi_files" and info.size == len(info.key) for info in listed)

        assert [info.key for info in await conn.file_list("fsi_files", prefix=f"{area[1:]}/p/")] == sorted(keys)
        assert [info.key for info in await conn.file_list("fsi_files", prefix=f"{area}/p/", start=f"{area}/p/3")] == [
            f"{area}/p/4",
            f"{area}/p/5",
        ]
        assert [info.key for info in await conn.file_list("fsi_files", prefix=f"{area}/p/", limit=2)] == [
            f"{area}/p/1",
            f"{area}/p/2",
        ]
        everything = [info.key for info in await conn.file_list("fsi_files")]
        assert everything == sorted(everything)
        assert set(keys) <= set(everything)
        assert await conn.file_list("fsi_files", prefix=f"{area}/none/") == []
    finally:
        for key in keys:
            await conn.file_delete(FileRef("fsi_files", key))


async def test_iter_files(conn: BaseSurrealConnection) -> None:
    """I2: seven files read in pages of three."""
    area = _area()
    keys = sorted(f"{area}/i/{n}" for n in range(7))
    for key in keys:
        await conn.file_put(FileRef("fsi_files", key), b"x")
    try:
        assert [info.key async for info in conn.iter_files("fsi_files", prefix=f"{area}/i/", batch_size=3)] == keys
        assert [info.key async for info in conn.iter_files("fsi_files", prefix=f"{area}/i/", batch_size=7)] == keys
    finally:
        for key in keys:
            await conn.file_delete(FileRef("fsi_files", key))


async def test_missing_bucket_raises(conn: BaseSurrealConnection) -> None:
    """I3: a missing bucket is a statement error, raised with the server's text."""
    ref = FileRef("fsi_nope", "/x")
    for call, sql in [
        (conn.file_put(ref, b"x"), "file::put($_file, $_data);"),
        (conn.file_get(ref), "RETURN file::get($_file);"),
        (conn.file_list("fsi_nope"), "RETURN file::list($_bucket, $_opts);"),
    ]:
        with pytest.raises(QueryError) as excinfo:
            await call
        assert type(excinfo.value) is QueryError
        assert "fsi_nope" in excinfo.value.message
        assert "does not exist" in excinfo.value.message
        assert excinfo.value.query == sql


async def test_read_only_bucket_write_raises(conn: BaseSurrealConnection) -> None:
    """I3: a write to a READONLY bucket raises; a read of it does not."""
    ref = FileRef("fsi_ro", "/x")
    with pytest.raises(QueryError, match="read-only"):
        await conn.file_put(ref, b"x")
    assert await conn.file_get(ref) is None


async def test_copy_of_missing_file_raises(conn: BaseSurrealConnection) -> None:
    """I3: copying a file that does not exist raises."""
    ref = FileRef("fsi_files", f"{_area()}/missing")
    with pytest.raises(QueryError, match="Source key does not exist"):
        await conn.file_copy(ref, "/target")
    with pytest.raises(QueryError, match="Source key does not exist"):
        await conn.file_rename(ref, "/target")


@pytest.fixture
async def record_user(files_conn: HTTPConnection, fsi_buckets: tuple[str, ...]) -> AsyncGenerator[HTTPConnection, None]:
    """An HTTP connection signed up as a record user; the access and its table are removed afterwards."""
    await files_conn.query(
        """
        DEFINE TABLE OVERWRITE fsi_user SCHEMALESS PERMISSIONS FULL;
        DEFINE ACCESS OVERWRITE fsi_access ON DATABASE TYPE RECORD
            SIGNUP (CREATE fsi_user SET name = $name)
            SIGNIN (SELECT * FROM fsi_user WHERE name = $name)
            DURATION FOR SESSION 1h;
        """
    )
    user = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    try:
        await user.connect()
        response = await user.signup(SURREALDB_NAMESPACE, SURREALDB_DATABASE, "fsi_access", name=uuid.uuid4().hex)
        assert response.token
        yield user
    finally:
        await user.close()
        await files_conn.query("REMOVE ACCESS IF EXISTS fsi_access ON DATABASE; REMOVE TABLE IF EXISTS fsi_user;")


async def test_bucket_permissions_for_a_record_user(files_conn: HTTPConnection, record_user: HTTPConnection) -> None:
    """I9: a bucket allowing only reads lets a record user read and refuses its write."""
    ref = FileRef("fsi_perm", f"{_area()}/readable")
    await files_conn.file_put(ref, b"public")
    try:
        assert await record_user.file_get(ref) == b"public"
        assert await record_user.file_exists(ref) is True
        with pytest.raises(QueryError) as excinfo:
            await record_user.file_put(ref, b"changed")
        assert type(excinfo.value) is QueryError
        assert "permission" in excinfo.value.message
        assert await files_conn.file_get(ref) == b"public"
    finally:
        await files_conn.file_delete(ref)


# ---------------------------------------------------------------------------
# I4 - request-size limits
# ---------------------------------------------------------------------------

MiB = 1024 * 1024


async def test_http_upload_over_the_limit_raises_before_sending(files_conn: HTTPConnection) -> None:
    """I4: 4 MiB + 1 over HTTP is refused client-side; the connection still works, the file was not written."""
    ref = FileRef("fsi_files", f"{_area()}/big.bin")
    with pytest.raises(PayloadTooLargeError) as excinfo:
        await files_conn.file_put(ref, os.urandom(4 * MiB + 1))
    assert excinfo.value.code == 413
    assert excinfo.value.limit == 4 * MiB
    assert await files_conn.file_exists(ref) is False
    await files_conn.file_put(ref, b"small")
    try:
        assert await files_conn.file_get(ref) == b"small"
    finally:
        await files_conn.file_delete(ref)


# ---------------------------------------------------------------------------
# I8 - JSON Lines export / import of a pointer column
# ---------------------------------------------------------------------------


async def test_jsonl_round_trips_a_pointer_column(conn: BaseSurrealConnection, tmp_path: Path) -> None:
    """I8: ``export_jsonl()`` writes pointers as ``$file`` tags and ``import_jsonl()`` binds them back."""
    suffix = uuid.uuid4().hex[:12]
    source, target = f"fsi_jsonl_src_{suffix}", f"fsi_jsonl_dst_{suffix}"
    area = _area()
    avatar = FileRef("fsi_files", f"{area}/a.png")
    thumbs = [FileRef("fsi_files", f"{area}/t1.png"), FileRef("fsi_ro", f"{area}/t2.png")]
    await conn.file_put(avatar, b"avatar bytes")
    try:
        response = await conn.query(
            f"CREATE {source}:a SET avatar = $avatar, thumbs = $thumbs; CREATE {source}:b SET avatar = NONE;",
            {"avatar": avatar, "thumbs": thumbs},
        )
        assert _statement_error(response) is None

        dest = tmp_path / "files.jsonl"
        assert await conn.export_jsonl(source, dest) == 2
        lines = dest.read_text(encoding="utf-8").splitlines()
        assert f'"avatar":{{"$file":["fsi_files","{area}/a.png"]}}' in lines[0]

        report = await conn.import_jsonl(target, dest)
        assert (report.lines, report.rows) == (2, 2)

        rows = (await conn.query(f"SELECT * FROM {target} ORDER BY id;")).first_result.records
        assert [row["id"] for row in rows] == [RecordId(target, "a"), RecordId(target, "b")]
        assert rows[0]["avatar"] == avatar
        assert isinstance(rows[0]["avatar"], FileRef)
        assert rows[0]["thumbs"] == thumbs
        assert "avatar" not in rows[1]
        # The stored value is a pointer, not text: a file function reads it in the database.
        stored = await conn.query(f"RETURN file::exists({target}:a.avatar); RETURN type::of({target}:a.avatar);")
        assert _statement_error(stored) is None
        assert [result.result for result in stored.results] == [True, "file"]
        assert await conn.file_get(rows[0]["avatar"]) == b"avatar bytes"
    finally:
        await conn.query(f"REMOVE TABLE IF EXISTS {source}; REMOVE TABLE IF EXISTS {target};")
        await conn.file_delete(avatar)


@pytest.fixture
async def ws_conn(files_conn: HTTPConnection, fsi_buckets: tuple[str, ...]) -> AsyncGenerator[WebSocketConnection, None]:
    """A root WebSocket connection of its own."""
    ws = WebSocketConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, auto_reconnect=False)
    await ws.connect()
    try:
        await ws.signin(SURREALDB_USER, SURREALDB_PASS)
        yield ws
    finally:
        await ws.close()


@pytest.mark.parametrize("size", [4 * MiB + 1, 5 * MiB], ids=["4MiB+1", "5MiB"])
async def test_websocket_upload_over_the_http_limit(ws_conn: WebSocketConnection, size: int) -> None:
    """I4: what HTTP refuses goes through a WebSocket (128 MiB by default) and round-trips."""
    ref = FileRef("fsi_files", f"{_area()}/big.bin")
    data = os.urandom(size)
    await ws_conn.file_put(ref, data)
    try:
        assert await ws_conn.file_get(ref) == data
        info = await ws_conn.file_head(ref)
        assert info is not None
        assert info.size == size
    finally:
        await ws_conn.file_delete(ref)


# ---------------------------------------------------------------------------
# I5 - FileField column through a migration, model round trip, filters, live()
# ---------------------------------------------------------------------------

I5_TABLE = "fsi_i5_docs"


class FSII5Doc(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=I5_TABLE, schema_mode="SCHEMAFULL")

    id: str | None = None
    title: str = ""
    avatar: FileField["fsi_files"]  # noqa: F821, UP037


@pytest.fixture
async def i5_table(files_conn: HTTPConnection, fsi_buckets: tuple[str, ...]) -> AsyncGenerator[TableState, None]:
    """The model's table, its columns created by AddField from the model introspector; the ORM pointed at it."""
    state = ModelIntrospector([FSII5Doc]).introspect().tables[I5_TABLE]
    sql = f"REMOVE TABLE IF EXISTS {I5_TABLE}; DEFINE TABLE {I5_TABLE} SCHEMAFULL; " + " ".join(
        AddField.from_field_state(I5_TABLE, field).forwards() for field in state.fields.values()
    )
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE, SURREALDB_DATABASE
    )
    try:
        error = _statement_error(await files_conn.query(sql))
        if error is not None:
            raise QueryError(error, query=sql)
        yield state
    finally:
        await files_conn.query(f"REMOVE TABLE IF EXISTS {I5_TABLE};")
        await SurrealDBConnectionManager.close_connection()
        await SurrealDBConnectionManager.unset_connection()


async def test_file_field_column_converges(files_conn: HTTPConnection, i5_table: TableState) -> None:
    """I5 / AC10: the server reports the column as `none | file<b>`, which parses back to the model's state."""
    response = await files_conn.query(f"INFO FOR TABLE {I5_TABLE};")
    assert _statement_error(response) is None
    assert response.first_result is not None
    definition = response.first_result.result["fields"]["avatar"]
    assert "none | file<fsi_files>" in definition
    assert parse_define_field(definition) == i5_table.fields["avatar"]


async def test_file_field_model_round_trip_and_filters(i5_table: TableState) -> None:
    """I5 / AC16: save and reload a FileRef; the pointer and its "bucket:/key" text both find the row."""
    ref = FileRef("fsi_files", f"{_area()}/avatar.png")
    await FSII5Doc(id="has_avatar", title="a", avatar=ref).save()
    await FSII5Doc(id="no_avatar", title="b").save()

    loaded = await FSII5Doc.objects().get("has_avatar")
    assert isinstance(loaded.avatar, FileRef)
    assert loaded.avatar == ref
    assert (await FSII5Doc.objects().get("no_avatar")).avatar is None

    async def ids(**filters: Any) -> list[str | None]:
        return [doc.id for doc in await FSII5Doc.objects().filter(**filters).exec()]

    assert await ids(avatar=ref) == ["has_avatar"]
    assert await ids(avatar=str(ref)) == ["has_avatar"]
    assert await ids(avatar__in=[str(ref)]) == ["has_avatar"]
    # Only membership: the server reads an absent value (NONE) as IS NOT NULL too,
    # so the row without an avatar matches as well, for any optional column.
    assert "has_avatar" in await ids(avatar__isnull=False)
    assert sorted(await ids(avatar__not_in=[str(ref)])) == ["no_avatar"]
    assert await FSII5Doc.objects().filter(Q(avatar=str(ref))).count() == 1


async def test_merge_with_pointer_text_writes_a_file_pointer(files_conn: HTTPConnection, i5_table: TableState) -> None:
    """merge() takes its keywords unvalidated: "bucket:/key" text must still reach a typed column as a pointer."""
    ref = FileRef("fsi_files", f"{_area()}/merged.png")
    doc = FSII5Doc(id="merged", title="a")
    await doc.save()

    await doc.merge(avatar=str(ref))

    assert doc.avatar == ref
    response = await files_conn.query(f"SELECT VALUE type::of(avatar) FROM {I5_TABLE}:merged;")
    assert _statement_error(response) is None
    assert response.first_result is not None
    assert response.first_result.result == ["file"]
    assert [d.id for d in await FSII5Doc.objects().filter(avatar=ref).exec()] == ["merged"]

    other = FileRef("fsi_files", f"{_area()}/in_tx.png")
    async with await SurrealDBConnectionManager.transaction() as tx:
        await doc.merge(tx=tx, avatar=str(other))
    assert [d.id for d in await FSII5Doc.objects().filter(avatar=other).exec()] == ["merged"]


I5_LOOSE_TABLE = "fsi_i5_loose"


class FSII5Loose(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=I5_LOOSE_TABLE)

    id: str | None = None
    avatar: FileField["fsi_files"]  # noqa: F821, UP037


async def test_merge_with_pointer_text_on_a_schemaless_table(files_conn: HTTPConnection, i5_table: TableState) -> None:
    """On a SCHEMALESS table the text used to be stored as a string, which no file filter matches."""
    ref = FileRef("fsi_files", f"{_area()}/loose.png")
    try:
        doc = FSII5Loose(id="loose")
        await doc.save()
        await doc.merge(avatar=str(ref))

        response = await files_conn.query(f"SELECT VALUE type::of(avatar) FROM {I5_LOOSE_TABLE}:loose;")
        assert _statement_error(response) is None
        assert response.first_result is not None
        assert response.first_result.result == ["file"]
        assert [d.id for d in await FSII5Loose.objects().filter(avatar=ref).exec()] == ["loose"]
        assert [d.id for d in await FSII5Loose.objects().filter(avatar=str(ref)).exec()] == ["loose"]
    finally:
        await files_conn.query(f"REMOVE TABLE IF EXISTS {I5_LOOSE_TABLE};")


async def test_file_field_rejects_another_bucket_before_any_query(i5_table: TableState) -> None:
    """I5: a pointer outside the field's buckets fails validation, so nothing reaches the server."""
    other = FileRef("fsi_ro", "/elsewhere.png")
    async with QueryLogger() as logger:
        with pytest.raises(ValidationError, match="not in the allowed bucket"):
            FSII5Doc(id="other", avatar=other)
        doc = FSII5Doc(id="other")
        with pytest.raises(ValidationError, match="not in the allowed bucket"):
            doc.avatar = other
    assert logger.total_queries == 0
    assert await FSII5Doc.objects().filter(id="other").count() == 0


async def test_file_field_live_filter_with_pointer_text(i5_table: TableState) -> None:
    """I5 / AC16: live() filtered with the text gets the matching UPDATE, then its DELETE.

    The server sends the deleted record with the DELETE, so the instance still
    carries the pointer; an id-only DELETE (no record) builds an instance whose
    file field reads None (unit test ``TestFileFieldLiveEvents``).
    """
    ref = FileRef("fsi_files", f"{_area()}/live.png")
    await FSII5Doc(id="hit", title="a", avatar=ref).save()
    await FSII5Doc(id="miss", title="a", avatar=FileRef("fsi_files", f"{_area()}/miss.png")).save()
    client = await SurrealDBConnectionManager.get_client()

    async with FSII5Doc.objects().filter(avatar=str(ref)).live() as stream:
        # The decoy is written first: a filter that wrongly matched it would deliver it first.
        for record in ("miss", "hit"):
            response = await client.query(f"UPDATE {I5_TABLE}:{record} SET title = 'b';")
            assert _statement_error(response) is None
        update = await asyncio.wait_for(anext(stream), timeout=5.0)
        assert update.action == LiveAction.UPDATE
        assert update.record_id == f"{I5_TABLE}:hit"
        assert update.instance.avatar == ref

        response = await client.query(f"DELETE {I5_TABLE}:hit;")
        assert _statement_error(response) is None
        deleted = await asyncio.wait_for(anext(stream), timeout=5.0)
        assert deleted.action == LiveAction.DELETE
        assert deleted.record_id == f"{I5_TABLE}:hit"
        assert deleted.instance.avatar == ref


# ---------------------------------------------------------------------------
# I6 - model file helpers (AC6)
# ---------------------------------------------------------------------------

I6_TABLE = "fsi_i6_docs"

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


class FSII6Doc(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=I6_TABLE, schema_mode="SCHEMAFULL")

    id: str | None = None
    title: str = ""
    doc: FileField["fsi_files"]  # noqa: F821, UP037
    photo: ImageField["fsi_files"]  # noqa: F821, UP037
    aliased: FileField["fsi_files"] = Field(default=None, alias="doc_col")  # noqa: F821, UP037


@pytest.fixture
async def i6_table(files_conn: HTTPConnection, fsi_buckets: tuple[str, ...]) -> AsyncGenerator[None, None]:
    """The model's table, its columns created by AddField from the model introspector; the ORM pointed at it.

    The introspector names a column after the Python field, not its alias, so
    the aliased field's column is renamed here to what the model writes.
    """
    state = ModelIntrospector([FSII6Doc]).introspect().tables[I6_TABLE]
    fields = [replace(field, name="doc_col") if field.name == "aliased" else field for field in state.fields.values()]
    sql = f"REMOVE TABLE IF EXISTS {I6_TABLE}; DEFINE TABLE {I6_TABLE} SCHEMAFULL; " + " ".join(
        AddField.from_field_state(I6_TABLE, field).forwards() for field in fields
    )
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE, SURREALDB_DATABASE
    )
    try:
        error = _statement_error(await files_conn.query(sql))
        if error is not None:
            raise QueryError(error, query=sql)
        yield
    finally:
        await files_conn.query(f"REMOVE TABLE IF EXISTS {I6_TABLE};")
        await SurrealDBConnectionManager.close_connection()
        await SurrealDBConnectionManager.unset_connection()


async def _i6_doc() -> FSII6Doc:
    """A saved record with an id of its own, so runs never share a default key."""
    doc = FSII6Doc(id=f"d{uuid.uuid4().hex[:12]}", title="a")
    await doc.save()
    return doc


async def _i6_row(files_conn: HTTPConnection, record_id: str) -> dict[str, Any]:
    """The record as the database holds it (column names, not field names)."""
    response = await files_conn.query("SELECT * FROM type::record($t, $id);", {"t": I6_TABLE, "id": record_id})
    assert _statement_error(response) is None
    rows = response.all_records
    assert len(rows) == 1
    row: dict[str, Any] = rows[0]
    return row


async def test_helpers_upload_download_info_delete(files_conn: HTTPConnection, i6_table: None) -> None:
    """I6: the default key, the record update, then download, file_info and delete_file."""
    doc = await _i6_doc()
    assert doc.id is not None
    ref = await doc.upload_file("doc", b"hello")
    assert ref == FileRef("fsi_files", f"/{I6_TABLE}/{doc.id}/doc")
    assert doc.doc == ref
    assert await files_conn.file_get(ref) == b"hello"

    loaded = await FSII6Doc.objects().get(doc.id)
    assert loaded.doc == ref
    assert await loaded.download_file("doc") == b"hello"
    info = await loaded.file_info("doc")
    assert info is not None
    assert (info.file, info.size) == (ref, 5)

    await loaded.delete_file("doc")
    assert loaded.doc is None
    assert await files_conn.file_exists(ref) is False
    assert "doc" not in await _i6_row(files_conn, doc.id)
    assert (await FSII6Doc.objects().get(doc.id)).doc is None
    assert await loaded.file_info("doc") is None


async def test_helpers_explicit_key_and_alias_column(files_conn: HTTPConnection, i6_table: None) -> None:
    """I6: an explicit key; an aliased field is written under its column."""
    doc = await _i6_doc()
    assert doc.id is not None
    key = f"{_area()}/report.txt"
    ref = await doc.upload_file("aliased", b"report", key=key)
    try:
        assert ref == FileRef("fsi_files", key)
        assert (await _i6_row(files_conn, doc.id))["doc_col"] == ref
        assert (await FSII6Doc.objects().get(doc.id)).aliased == ref
        assert await doc.download_file("aliased") == b"report"
    finally:
        await files_conn.file_delete(ref)


async def test_helpers_image_field(files_conn: HTTPConnection, i6_table: None) -> None:
    """I6: an ImageField takes an image and refuses text before any request."""
    doc = await _i6_doc()
    async with QueryLogger() as logger:
        with pytest.raises(ValueError, match="accepts PNG, JPEG, GIF or WebP data only"):
            await doc.upload_file("photo", b"just some text")
    assert logger.total_queries == 0
    assert doc.photo is None

    ref = await doc.upload_file("photo", PNG_HEADER + b"pixels")
    try:
        assert await files_conn.file_get(ref) == PNG_HEADER + b"pixels"
    finally:
        await files_conn.file_delete(ref)


async def test_helpers_large_upload_goes_over_websocket(files_conn: HTTPConnection, i6_table: None) -> None:
    """I6: 5 MiB is over the HTTP request limit, so the upload uses the connection's WebSocket."""
    doc = await _i6_doc()
    data = os.urandom(5 * MiB)
    assert "default" not in SurrealDBConnectionManager._ws_clients
    ref = await doc.upload_file("doc", data)
    try:
        assert "default" in SurrealDBConnectionManager._ws_clients
        assert await doc.download_file("doc") == data
        info = await doc.file_info("doc")
        assert info is not None
        assert info.size == 5 * MiB
    finally:
        await files_conn.file_delete(ref)


async def test_helpers_overwrite_false_onto_an_existing_file(files_conn: HTTPConnection, i6_table: None) -> None:
    """I6: a second overwrite=False upload onto the default key raises and changes nothing."""
    doc = await _i6_doc()
    assert doc.id is not None
    ref = await doc.upload_file("doc", b"first", overwrite=False)
    try:
        with pytest.raises(FileExistsError) as info:
            await doc.upload_file("doc", b"second", overwrite=False)
        assert str(info.value) == f"{ref} already exists: pass overwrite=True or another key="
        assert await files_conn.file_get(ref) == b"first"
        assert (await FSII6Doc.objects().get(doc.id)).doc == ref
        assert doc.doc == ref
    finally:
        await files_conn.file_delete(ref)


# ---------------------------------------------------------------------------
# I7 - bucket migrations through the server (AC8, AC9)
# ---------------------------------------------------------------------------

I7_BUCKETS = ("fsi_i7_plain", "fsi_i7_ro", "fsi_i7-dash", "fsi_i7_where", "fsi_i7_alter", "fsi_i7_gone", "fsi_i7_dbonly")


@pytest.fixture
async def i7_buckets(files_conn: HTTPConnection, fsi_buckets: tuple[str, ...]) -> AsyncGenerator[None, None]:
    """Remove the I7 buckets before and after each test."""
    sql = " ".join(f"REMOVE BUCKET IF EXISTS {escape_record_id(name)};" for name in I7_BUCKETS)
    await files_conn.query(sql)
    try:
        yield
    finally:
        await files_conn.query(sql)


async def _apply(conn: HTTPConnection, sql: str) -> None:
    error = _statement_error(await conn.query(sql))
    if error is not None:
        raise QueryError(error, query=sql)


async def _db_buckets(conn: HTTPConnection) -> dict[str, BucketState]:
    state = await DatabaseIntrospector(connection=conn).introspect()
    assert state.buckets is not None
    return state.buckets


def _state_of(op: DefineBucket) -> BucketState:
    return BucketState(
        name=op.name,
        backend=op.backend,
        readonly=op.readonly,
        permissions=op.permissions or "FULL",
        comment=op.comment,
    )


async def test_bucket_definitions_read_back_equal(files_conn: HTTPConnection, i7_buckets: None) -> None:
    """I7: each DefineBucket form, read back through INFO FOR DB, equals the operation's state."""
    operations = [
        DefineBucket("fsi_i7_plain", backend="memory"),
        DefineBucket("fsi_i7_ro", backend="memory", readonly=True, permissions="FULL"),
        DefineBucket("fsi_i7-dash", backend="memory", permissions="NONE", comment="it's"),
        DefineBucket(
            "fsi_i7_where",
            backend="memory",
            permissions="$action INSIDE ['get', 'head'] OR $auth.role = 'admin'",
            comment="back\\slash COMMENT 'x'",
        ),
    ]
    await _apply(files_conn, " ".join(op.forwards() for op in operations))
    buckets = await _db_buckets(files_conn)
    for op in operations:
        assert buckets[op.name] == _state_of(op), op.name
        assert buckets[op.name].permissions == _state_of(op).permissions


async def test_alter_bucket_and_its_rollback(files_conn: HTTPConnection, i7_buckets: None) -> None:
    """I7: AlterBucket replaces the definition; its rollback restores the previous one."""
    current = BucketState(name="fsi_i7_alter", backend="memory", comment="old")
    target = BucketState(name="fsi_i7_alter", backend="memory", readonly=True, permissions="NONE")
    await _apply(files_conn, DefineBucket.from_bucket_state(current).forwards())
    assert (await _db_buckets(files_conn))["fsi_i7_alter"] == current

    alter = AlterBucket.from_bucket_states(current, target)
    assert alter.reversible
    await _apply(files_conn, alter.forwards())
    assert (await _db_buckets(files_conn))["fsi_i7_alter"] == target
    await _apply(files_conn, alter.backwards())
    assert (await _db_buckets(files_conn))["fsi_i7_alter"] == current


async def test_remove_bucket(files_conn: HTTPConnection, i7_buckets: None) -> None:
    """I7: RemoveBucket drops the definition."""
    await _apply(files_conn, DefineBucket("fsi_i7_gone", backend="memory").forwards())
    assert "fsi_i7_gone" in await _db_buckets(files_conn)
    await _apply(files_conn, RemoveBucket("fsi_i7_gone").forwards())
    assert "fsi_i7_gone" not in await _db_buckets(files_conn)


async def test_diff_against_models_leaves_buckets_alone(files_conn: HTTPConnection, i7_buckets: None) -> None:
    """I7 / D17: models never declare buckets, so schema_diff() proposes none; a target listing none removes it."""
    await _apply(files_conn, DefineBucket("fsi_i7_dbonly", backend="memory").forwards())

    operations = await schema_diff(models=[FSII5Doc], connection=files_conn)
    assert operations  # the model's table is not defined here
    assert not [op for op in operations if isinstance(op, (DefineBucket, AlterBucket, RemoveBucket))]

    db_state = await DatabaseIntrospector(connection=files_conn).introspect()
    removals = [op for op in db_state.diff(SchemaState(buckets={})) if isinstance(op, RemoveBucket)]
    removal = next(op for op in removals if op.name == "fsi_i7_dbonly")
    await _apply(files_conn, removal.forwards())
    assert "fsi_i7_dbonly" not in await _db_buckets(files_conn)
