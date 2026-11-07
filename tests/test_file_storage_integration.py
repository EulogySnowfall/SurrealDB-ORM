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

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

import pytest

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
