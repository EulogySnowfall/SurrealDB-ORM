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
from collections.abc import AsyncGenerator

import pytest

from surreal_sdk import FileRef, HTTPConnection
from surreal_sdk.exceptions import QueryError
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
