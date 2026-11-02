"""Unit tests for the SDK's JSON Lines batch I/O: export (``iter_records()``, ``export_jsonl()``, U6) and import
(``import_jsonl()``, U7), the HTTP database dump (``export_database*()``, U8) and restore (``import_database()``, U9).

A fake connection records every ``query()`` call and answers from a script, so
the statements, the bound cursor and the stop conditions are checked without a
server. Records are built on demand and tracked by weak reference, which is how
"never more than one batch held" is asserted.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import os
import stat
import sys
import weakref
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal, Self

import httpx
import pytest

from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.exceptions import (
    ConnectionError,
    DataFormatError,
    DataImportError,
    QueryError,
    TableNotFoundError,
    TimeoutError,
)
from surreal_sdk.jsonl import ImportReport, _iter_batches, dumps_record, loads_record
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.protocol.rpc import RPCRequest, RPCResponse
from surreal_sdk.types import QueryResponse

C1 = "SELECT * FROM t ORDER BY id LIMIT {n};"
C2 = "SELECT * FROM t WHERE id > $_x_after ORDER BY id LIMIT {n};"


class Row(dict[str, Any]):
    """A record that can be tracked by weak reference (a plain ``dict`` cannot)."""


Answer = Callable[[str, dict[str, Any]], Any]


class FakeConnection(BaseSurrealConnection):
    """Records ``query()`` calls; ``answer(sql, vars)`` returns the RPC result or raises."""

    def __init__(self, answer: Answer, *, protocol: Literal["json", "cbor"] = "cbor") -> None:
        super().__init__("http://fake", "ns", "db")
        self.protocol = protocol
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> Self:
        return self

    async def close(self) -> None:
        return None

    async def _send_rpc(self, request: RPCRequest) -> RPCResponse:
        raise AssertionError("the batch loop goes through query()")

    def transaction(self) -> Any:
        raise AssertionError("not used")

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        self.calls.append((sql, dict(vars or {})))
        return QueryResponse.from_rpc_result(self.answer(sql, dict(vars or {})))


def ok(records: list[Any]) -> list[dict[str, Any]]:
    return [{"status": "OK", "result": records, "time": ""}]


def table_rows(count: int) -> Callable[[str, dict[str, Any]], Any]:
    """A table ``t`` holding ids ``t:1`` .. ``t:<count>``, answered by keyset like the server."""

    def answer(sql: str, vars: dict[str, Any]) -> Any:
        n = int(sql.rsplit("LIMIT ", 1)[1].rstrip(";"))
        after = vars["_x_after"].id if "_x_after" in vars else 0
        ids = [i for i in range(1, count + 1) if i > after][:n]
        return ok([Row(id=RecordId("t", i), v=i) for i in ids])

    return answer


async def collect(conn: BaseSurrealConnection, table: str = "t", **kwargs: Any) -> list[list[dict[str, Any]]]:
    return [batch async for batch in conn.iter_records(table, **kwargs)]


# ---------------------------------------------------------------------------
# Statements and cursor
# ---------------------------------------------------------------------------


async def test_first_batch_is_c1_then_c2_with_last_id_bound() -> None:
    conn = FakeConnection(table_rows(5))
    batches = await collect(conn, batch_size=2)

    assert [[r["v"] for r in b] for b in batches] == [[1, 2], [3, 4], [5]]
    assert [sql for sql, _ in conn.calls] == [C1.format(n=2), C2.format(n=2), C2.format(n=2)]
    assert conn.calls[0][1] == {}
    assert conn.calls[1][1] == {"_x_after": RecordId("t", 2)}
    assert conn.calls[2][1] == {"_x_after": RecordId("t", 4)}


async def test_cursor_is_the_decoded_id_object_itself() -> None:
    last = RecordId("t", [1, "a"])

    def answer(sql: str, vars: dict[str, Any]) -> Any:
        return ok([{"id": last}]) if "_x_after" not in vars else ok([])

    conn = FakeConnection(answer)
    await collect(conn, batch_size=1)
    assert conn.calls[1][1]["_x_after"] is last


async def test_batches_are_plain_dicts() -> None:
    conn = FakeConnection(table_rows(3))
    batches = await collect(conn, batch_size=10)
    assert len(batches) == 1
    assert all(isinstance(r, dict) for r in batches[0])
    assert isinstance(batches[0], list)


async def test_exact_multiple_ends_on_an_empty_batch() -> None:
    conn = FakeConnection(table_rows(4))
    batches = await collect(conn, batch_size=2)
    assert [len(b) for b in batches] == [2, 2]
    assert len(conn.calls) == 3  # the third reply is empty and is not yielded


async def test_short_batch_stops() -> None:
    conn = FakeConnection(table_rows(3))
    await collect(conn, batch_size=5)
    assert len(conn.calls) == 1


async def test_where_renders_c1w_and_c2w_and_binds_vars() -> None:
    conn = FakeConnection(table_rows(3))
    await collect(conn, batch_size=2, where="v >= $min OR x = 1", vars={"min": 0})

    assert conn.calls[0] == ("SELECT * FROM t WHERE (v >= $min OR x = 1) ORDER BY id LIMIT 2;", {"min": 0})
    assert conn.calls[1] == (
        "SELECT * FROM t WHERE (v >= $min OR x = 1) AND id > $_x_after ORDER BY id LIMIT 2;",
        {"min": 0, "_x_after": RecordId("t", 2)},
    )


async def test_limit_caps_n_and_stops() -> None:
    conn = FakeConnection(table_rows(10))
    batches = await collect(conn, batch_size=2, limit=5)

    assert sum(len(b) for b in batches) == 5
    assert [sql for sql, _ in conn.calls] == [C1.format(n=2), C2.format(n=2), C2.format(n=1)]


async def test_limit_reached_sends_no_further_query() -> None:
    conn = FakeConnection(table_rows(10))
    await collect(conn, batch_size=2, limit=4)
    assert len(conn.calls) == 2


async def test_limit_below_batch_size() -> None:
    conn = FakeConnection(table_rows(10))
    batches = await collect(conn, batch_size=1000, limit=3)
    assert [len(b) for b in batches] == [3]
    assert [sql for sql, _ in conn.calls] == [C1.format(n=3)]


async def test_limit_zero_sends_nothing() -> None:
    conn = FakeConnection(table_rows(10))
    assert await collect(conn, limit=0) == []
    assert conn.calls == []


# ---------------------------------------------------------------------------
# Statement errors and request failures
# ---------------------------------------------------------------------------


async def test_err_result_raises_query_error() -> None:
    conn = FakeConnection(lambda sql, vars: [{"status": "ERR", "result": "Parse error: boom", "time": ""}])
    with pytest.raises(QueryError, match="Parse error: boom") as info:
        await collect(conn)
    assert not isinstance(info.value, TableNotFoundError)
    assert info.value.query == C1.format(n=1000)


async def test_missing_table_raises_table_not_found() -> None:
    message = "The table 'missing_tbl' does not exist"
    conn = FakeConnection(lambda sql, vars: [{"status": "ERR", "result": message, "time": ""}])
    with pytest.raises(TableNotFoundError, match="missing_tbl"):
        await collect(conn, "missing_tbl")


@pytest.mark.parametrize(
    "error",
    [QueryError("HTTP 400: bad", code=400), ConnectionError("Request failed: reset"), TimeoutError("timed out")],
    ids=["http-400", "connection", "timeout"],
)
async def test_request_failure_on_batch_two_propagates_unchanged(error: Exception) -> None:
    rows = table_rows(5)

    def answer(sql: str, vars: dict[str, Any]) -> Any:
        if "_x_after" in vars:
            raise error
        return rows(sql, vars)

    conn = FakeConnection(answer)
    seen: list[list[dict[str, Any]]] = []
    with pytest.raises(type(error)) as info:
        async for batch in conn.iter_records("t", batch_size=2):
            seen.append(batch)
    assert info.value is error
    assert [[r["v"] for r in b] for b in seen] == [[1, 2]]


# ---------------------------------------------------------------------------
# Argument checks (raised at the call, before any query)
# ---------------------------------------------------------------------------


def test_json_protocol_is_refused_e4() -> None:
    conn = FakeConnection(table_rows(1), protocol="json")
    with pytest.raises(ValueError, match=r"^iter_records\(\) needs the CBOR protocol: under JSON, record ids"):
        conn.iter_records("t")
    assert conn.calls == []


@pytest.mark.parametrize("table", ["t y", "t\n", "", "1t", "t;", 5])
def test_invalid_table_e5(table: Any) -> None:
    conn = FakeConnection(table_rows(1))
    with pytest.raises(ValueError, match=r"^Invalid table name: .* \(expected an identifier\)$"):
        conn.iter_records(table)


@pytest.mark.parametrize(
    ("kwargs", "exc", "text"),
    [
        ({"batch_size": 0}, ValueError, "batch_size must be >= 1, got 0"),
        ({"batch_size": True}, TypeError, "batch_size must be an int, got bool"),
        ({"batch_size": 2.0}, TypeError, "batch_size must be an int, got float"),
        ({"limit": -1}, ValueError, "limit must be >= 0, got -1"),
        ({"limit": "5"}, TypeError, "limit must be an int, got str"),
    ],
)
def test_int_arguments_e1(kwargs: dict[str, Any], exc: type[Exception], text: str) -> None:
    conn = FakeConnection(table_rows(1))
    with pytest.raises(exc) as info:
        conn.iter_records("t", **kwargs)
    assert str(info.value) == text


def test_reserved_variable_names_e14() -> None:
    conn = FakeConnection(table_rows(1))
    with pytest.raises(ValueError) as info:
        conn.iter_records("t", where="id > $_x_after", vars={"_x_after": 1})
    assert str(info.value) == "Variable names starting with '_x_' are reserved for data export/import: '_x_after'"


async def test_export_checks_arguments_before_opening_the_sink(tmp_path: Path) -> None:
    dest = tmp_path / "out.jsonl"
    conn = FakeConnection(table_rows(1))
    with pytest.raises(ValueError, match="Invalid table name"):
        await conn.export_jsonl("t y", dest)
    with pytest.raises(ValueError, match=r"^export_jsonl\(\) needs the CBOR protocol"):
        await FakeConnection(table_rows(1), protocol="json").export_jsonl("t", dest)
    with pytest.raises(ValueError, match="reserved"):
        await conn.export_jsonl("t", dest, vars={"_x_rows": 1})
    assert not dest.exists()
    assert conn.calls == []


# ---------------------------------------------------------------------------
# _iter_batches(prelude=...) and on_query
# ---------------------------------------------------------------------------


async def test_prelude_renders_c1p_c2p_and_reads_the_last_result() -> None:
    prelude = ["LET $_sq0 = (SELECT VALUE id FROM sq ORDER BY v DESC LIMIT 4);"]
    rows = table_rows(3)

    def answer(sql: str, vars: dict[str, Any]) -> Any:
        return [{"status": "OK", "result": None, "time": ""}, *rows(sql, vars)]

    conn = FakeConnection(answer)
    batches = [
        b
        async for b in _iter_batches(
            conn,
            "t",
            batch_size=2,
            where_sql="id IN $_sq0",
            vars={"_f0": 1},
            limit=None,
            method="iter_jsonl",
            prelude=prelude,
        )
    ]

    assert [[r["v"] for r in b] for b in batches] == [[1, 2], [3]]
    assert conn.calls[0] == (
        "LET $_sq0 = (SELECT VALUE id FROM sq ORDER BY v DESC LIMIT 4); SELECT * FROM t WHERE id IN $_sq0 ORDER BY id LIMIT 2;",
        {"_f0": 1},
    )
    assert conn.calls[1] == (
        "LET $_sq0 = (SELECT VALUE id FROM sq ORDER BY v DESC LIMIT 4); "
        "SELECT * FROM t WHERE id IN $_sq0 AND id > $_x_after ORDER BY id LIMIT 2;",
        {"_f0": 1, "_x_after": RecordId("t", 2)},
    )


async def test_err_on_the_prelude_raises_query_error() -> None:
    def answer(sql: str, vars: dict[str, Any]) -> Any:
        return [{"status": "ERR", "result": "LET failed", "time": ""}, *ok([{"id": RecordId("t", 1)}])]

    conn = FakeConnection(answer)
    with pytest.raises(QueryError, match="LET failed"):
        async for _ in _iter_batches(
            conn, "t", batch_size=2, where_sql=None, vars={}, limit=None, method="iter_jsonl", prelude=["LET $a = 1;"]
        ):
            pass


async def test_on_query_called_once_per_batch_with_the_sent_text() -> None:
    logged: list[tuple[str, dict[str, Any], float]] = []
    conn = FakeConnection(table_rows(3))
    await collect(conn, batch_size=2, _on_query=lambda sql, variables, ms: logged.append((sql, variables, ms)))

    assert [(sql, variables) for sql, variables, _ in logged] == [
        (C1.format(n=2), {}),
        (C2.format(n=2), {"_x_after": RecordId("t", 2)}),
    ]
    assert all(isinstance(ms, float) and ms >= 0 for _, _, ms in logged)


async def test_on_query_not_called_when_the_request_raises() -> None:
    logged: list[str] = []

    def answer(sql: str, vars: dict[str, Any]) -> Any:
        raise ConnectionError("Request failed: reset")

    conn = FakeConnection(answer)
    with pytest.raises(ConnectionError):
        await collect(conn, _on_query=lambda sql, variables, ms: logged.append(sql))
    assert logged == []


# ---------------------------------------------------------------------------
# One batch held at a time
# ---------------------------------------------------------------------------


def tracked_table(count: int, alive_at_query: list[int]) -> Answer:
    """Like :func:`table_rows`, recording how many earlier batches are still alive at each query."""
    refs: list[list[weakref.ref[Row]]] = []
    rows = table_rows(count)

    def answer(sql: str, vars: dict[str, Any]) -> Any:
        gc.collect()
        alive_at_query.append(sum(1 for batch in refs if any(ref() is not None for ref in batch)))
        result = rows(sql, vars)
        refs.append([weakref.ref(r) for r in result[0]["result"]])
        return result

    return answer


async def test_iter_records_holds_at_most_one_earlier_batch() -> None:
    alive: list[int] = []
    conn = FakeConnection(tracked_table(9, alive))
    async for batch in conn.iter_records("t", batch_size=2):
        assert len(batch) <= 2
        del batch
    assert len(alive) == 5
    assert max(alive) <= 1


async def test_export_holds_at_most_one_earlier_batch(tmp_path: Path) -> None:
    alive: list[int] = []
    conn = FakeConnection(tracked_table(9, alive))
    assert await conn.export_jsonl("t", tmp_path / "out.jsonl", batch_size=2) == 9
    assert len(alive) == 5
    assert max(alive) <= 1


# ---------------------------------------------------------------------------
# export_jsonl()
# ---------------------------------------------------------------------------


@pytest.fixture
def thread_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the name of every function sent to ``asyncio.to_thread``."""
    names: list[str] = []
    real = asyncio.to_thread

    async def spy(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        names.append(getattr(func, "__name__", repr(func)))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    return names


async def test_export_to_a_path_writes_one_line_per_record(tmp_path: Path, thread_spy: list[str]) -> None:
    dest = tmp_path / "out.jsonl"
    conn = FakeConnection(table_rows(5))

    count = await conn.export_jsonl("t", dest, batch_size=2)

    assert count == 5
    data = dest.read_bytes()
    assert data.endswith(b"\n") and b"\r" not in data
    lines = data.decode("utf-8").splitlines()
    assert [loads_record(line) for line in lines] == [{"id": RecordId("t", i), "v": i} for i in range(1, 6)]
    # open, one write per batch (3), close: all off the event loop
    assert len(thread_spy) == 5


async def test_export_overwrites_an_existing_file(tmp_path: Path) -> None:
    dest = tmp_path / "out.jsonl"
    dest.write_text("old\nold\nold\nold\n", encoding="utf-8")
    conn = FakeConnection(table_rows(1))
    assert await conn.export_jsonl("t", str(dest)) == 1
    assert dest.read_text(encoding="utf-8").count("\n") == 1


@pytest.fixture
def umask_022() -> Any:
    """Run the test under umask ``0o022``, where a plain ``open()`` would create ``0o644``."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.usefixtures("umask_022")
async def test_export_creates_a_new_file_owner_only(tmp_path: Path) -> None:
    dest = tmp_path / "new.jsonl"

    assert await FakeConnection(table_rows(2)).export_jsonl("t", str(dest)) == 2

    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.usefixtures("umask_022")
async def test_export_truncates_an_owner_only_file_without_widening_its_mode(tmp_path: Path) -> None:
    dest = tmp_path / "existing.jsonl"
    dest.write_text("an older and much longer export\n" * 10, encoding="utf-8")
    dest.chmod(0o600)

    assert await FakeConnection(table_rows(1)).export_jsonl("t", dest) == 1

    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert dest.read_text(encoding="utf-8") == dumps_record({"id": RecordId("t", 1), "v": 1}) + "\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
async def test_export_keeps_the_mode_of_an_existing_file(tmp_path: Path) -> None:
    dest = tmp_path / "shared.jsonl"
    dest.write_text("old\n", encoding="utf-8")
    dest.chmod(0o640)

    await FakeConnection(table_rows(1)).export_jsonl("t", dest)

    assert stat.S_IMODE(dest.stat().st_mode) == 0o640


def test_dump_and_jsonl_export_share_one_file_opener() -> None:
    from surreal_sdk import jsonl, utils
    from surreal_sdk.connection import http

    assert http._open_owner_only is utils._open_owner_only
    assert jsonl._open_owner_only is utils._open_owner_only
    assert "open_owner_only" not in utils.__all__
    assert "_open_owner_only" not in utils.__all__


def test_owner_only_opener_rejects_another_mode(tmp_path: Path) -> None:
    from surreal_sdk.utils import _open_owner_only

    dest = tmp_path / "never.txt"
    with pytest.raises(ValueError, match="mode must be"):
        _open_owner_only(str(dest), "a")  # type: ignore[call-overload]
    assert not dest.exists()


async def test_export_to_a_text_stream_is_not_closed(thread_spy: list[str]) -> None:
    sink = io.StringIO()
    conn = FakeConnection(table_rows(3))

    count = await conn.export_jsonl("t", sink, batch_size=2)

    assert count == 3
    assert not sink.closed
    lines = sink.getvalue().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"id": {"$record": ["t", 1]}, "v": 1}
    assert len(thread_spy) == 2  # one write per batch, no open/close


async def test_export_relaxed_form() -> None:
    sink = io.StringIO()
    conn = FakeConnection(table_rows(1))
    await conn.export_jsonl("t", sink, typed=False)
    assert json.loads(sink.getvalue()) == {"id": "t:1", "v": 1}


async def test_export_empty_table_creates_an_empty_file(tmp_path: Path) -> None:
    dest = tmp_path / "out.jsonl"
    conn = FakeConnection(table_rows(0))
    assert await conn.export_jsonl("t", dest) == 0
    assert dest.read_bytes() == b""


async def test_export_limit_zero_writes_nothing_and_sends_nothing() -> None:
    sink = io.StringIO()
    conn = FakeConnection(table_rows(3))
    assert await conn.export_jsonl("t", sink, limit=0) == 0
    assert sink.getvalue() == ""
    assert conn.calls == []


async def test_export_passes_where_vars_limit_and_on_query() -> None:
    logged: list[str] = []
    conn = FakeConnection(table_rows(10))
    count = await conn.export_jsonl(
        "t", io.StringIO(), batch_size=2, where="v > $m", vars={"m": 0}, limit=3, _on_query=lambda s, v, ms: logged.append(s)
    )
    assert count == 3
    assert logged == [
        "SELECT * FROM t WHERE (v > $m) ORDER BY id LIMIT 2;",
        "SELECT * FROM t WHERE (v > $m) AND id > $_x_after ORDER BY id LIMIT 1;",
    ]


async def test_export_error_leaves_the_partial_file_and_closes_it(tmp_path: Path) -> None:
    dest = tmp_path / "out.jsonl"
    rows = table_rows(5)
    error = ConnectionError("Request failed: reset")

    def answer(sql: str, vars: dict[str, Any]) -> Any:
        if "_x_after" in vars:
            raise error
        return rows(sql, vars)

    conn = FakeConnection(answer)
    with pytest.raises(ConnectionError) as info:
        await conn.export_jsonl("t", dest, batch_size=2)
    assert info.value is error
    assert len(dest.read_text(encoding="utf-8").splitlines()) == 2


async def test_export_refuses_a_tuple_value_e2() -> None:
    conn = FakeConnection(lambda sql, vars: ok([{"id": RecordId("t", 1), "loc": (1.5, 2.5)}]))
    with pytest.raises(DataFormatError, match=r"^Cannot write loc of t:1 as JSON Lines: a tuple"):
        await conn.export_jsonl("t", io.StringIO())


# ---------------------------------------------------------------------------
# import_jsonl() (U7)
# ---------------------------------------------------------------------------

C3 = "INSERT INTO t $_x_rows RETURN NONE;"
C4 = "INSERT IGNORE INTO t $_x_rows RETURN NONE;"
C5 = "INSERT RELATION INTO t $_x_rows RETURN NONE;"
C6 = "INSERT RELATION IGNORE INTO t $_x_rows RETURN NONE;"
C10 = "RETURN (INFO FOR DB).tables[$_x_table];"


def line(i: int, **extra: Any) -> str:
    return dumps_record({"id": RecordId("t", i), "v": i, **extra})


def edge(i: int) -> str:
    return dumps_record({"id": RecordId("t", i), "in": RecordId("p", 1), "out": RecordId("p", 2)})


def importer(table_info: Any = None, *, fail: Callable[[int], Any] | None = None) -> Answer:
    """Answer C10 with ``table_info`` (a list of results, or a text) and each INSERT with ``OK []``.

    ``fail(batch_number)`` (1-based) may raise, or return results to send instead.
    """
    batch = 0

    def answer(sql: str, vars: dict[str, Any]) -> Any:
        nonlocal batch
        if sql == C10:
            if isinstance(table_info, list):
                return table_info
            return [{"status": "OK", "result": table_info, "time": ""}]
        batch += 1
        if fail is not None:
            scripted = fail(batch)
            if scripted is not None:
                return scripted
        results = [{"status": "OK", "result": [], "time": ""}]
        if sql.startswith("OPTION IMPORT; "):
            results.insert(0, {"status": "OK", "result": None, "time": ""})
        return results

    return answer


def inserts(conn: FakeConnection) -> list[tuple[str, dict[str, Any]]]:
    return [(sql, vars) for sql, vars in conn.calls if sql != C10]


async def test_import_batches_by_rows_and_reports_counts() -> None:
    conn = FakeConnection(importer())

    report = await conn.import_jsonl("t", [line(i) for i in range(1, 6)], batch_size=2, relation=False)

    assert report == ImportReport(table="t", lines=5, rows=5, batches=3)
    assert [sql for sql, _ in conn.calls] == [C3, C3, C3]
    sent = [[row["v"] for row in vars["_x_rows"]] for _, vars in conn.calls]
    assert sent == [[1, 2], [3, 4], [5]]
    assert conn.calls[0][1]["_x_rows"][0] == {"id": RecordId("t", 1), "v": 1}
    assert set(conn.calls[0][1]) == {"_x_rows"}


async def test_import_batches_by_bytes_and_sends_an_oversized_line_alone() -> None:
    small = [line(i, pad="x" * 50) for i in range(1, 4)]
    big = line(4, pad="x" * 500)
    size = len(small[0].encode("utf-8"))
    conn = FakeConnection(importer())
    logged: list[dict[str, Any]] = []

    report = await conn.import_jsonl(
        "t",
        [*small, big, line(5, pad="x" * 50)],
        max_batch_bytes=2 * size + 1,
        relation=False,
        _on_query=lambda sql, vars, ms: logged.append(vars),
    )

    sent = [[row["v"] for row in vars["_x_rows"]] for _, vars in conn.calls]
    assert sent == [[1, 2], [3], [4], [5]]
    assert report.batches == 4 and report.rows == 5
    assert [entry["_x_rows"]["bytes"] for entry in logged][:2] == [2 * size, size]
    assert logged[2]["_x_rows"] == {"rows": 1, "bytes": len(big.encode("utf-8"))}


async def test_import_on_query_gets_a_summary_never_the_rows() -> None:
    lines = [line(i) for i in range(1, 4)]
    conn = FakeConnection(importer())
    logged: list[tuple[str, dict[str, Any]]] = []

    await conn.import_jsonl("t", lines, batch_size=2, relation=False, _on_query=lambda s, v, ms: logged.append((s, v)))

    assert logged == [
        (C3, {"_x_rows": {"rows": 2, "bytes": len(lines[0]) + len(lines[1])}}),
        (C3, {"_x_rows": {"rows": 1, "bytes": len(lines[2])}}),
    ]


async def test_import_skips_blank_lines_and_counts_them_in_line_numbers() -> None:
    def reject(batch: int) -> Any:
        return [{"status": "ERR", "result": "boom", "time": ""}] if batch == 2 else None

    conn = FakeConnection(importer(fail=reject))
    source = [line(1), "", "   ", line(2), "\n", line(3), line(4)]

    with pytest.raises(DataImportError) as info:
        await conn.import_jsonl("t", source, batch_size=2, relation=False)

    assert str(info.value) == "Batch at lines 6-7 was rejected by t: boom"
    # lines counts every decoded non-blank line, the rejected batch's included.
    assert info.value.report == ImportReport(table="t", lines=4, rows=2, batches=1)


async def test_import_invalid_line_names_its_number_e3() -> None:
    conn = FakeConnection(importer())
    with pytest.raises(DataFormatError, match=r"^Line 3: invalid JSON") as info:
        await conn.import_jsonl("t", [line(1), "", "{nope"], relation=False)
    assert info.value.line == 3


async def test_import_unknown_tag_names_its_line_e3() -> None:
    conn = FakeConnection(importer())
    with pytest.raises(DataFormatError, match=r"^Line 2: unknown tag '\$recrod'"):
        await conn.import_jsonl("t", [line(1), '{"id": {"$recrod": ["t", 2]}}'], relation=False)


@pytest.mark.parametrize(
    ("on_conflict", "relation", "statement"),
    [("error", False, C3), ("ignore", False, C4), ("error", True, C5), ("ignore", True, C6)],
)
async def test_import_statement_selection_c3_to_c6(on_conflict: Any, relation: bool, statement: str) -> None:
    conn = FakeConnection(importer())
    await conn.import_jsonl("t", [line(1)], on_conflict=on_conflict, relation=relation)
    assert [sql for sql, _ in conn.calls] == [statement]


@pytest.mark.parametrize(("on_conflict", "relation", "statement"), [("error", False, C3), ("ignore", True, C6)])
async def test_import_raw_prefixes_option_import_c7(on_conflict: Any, relation: bool, statement: str) -> None:
    conn = FakeConnection(importer())
    await conn.import_jsonl("t", [line(1)], on_conflict=on_conflict, relation=relation, raw=True)
    assert [sql for sql, _ in conn.calls] == ["OPTION IMPORT; " + statement]


async def test_import_raw_checks_the_insert_result_the_second_one() -> None:
    def reject(batch: int) -> Any:
        return [
            {"status": "OK", "result": None, "time": ""},
            {"status": "ERR", "result": "Database record `t:1` already exists", "time": ""},
        ]

    conn = FakeConnection(importer(fail=reject))
    with pytest.raises(DataImportError, match=r"^Batch at lines 1-1 was rejected by t: Database record `t:1` already exists$"):
        await conn.import_jsonl("t", [line(1)], relation=False, raw=True)


async def test_import_e8_carries_the_report_of_the_accepted_batches() -> None:
    message = "Found 'x' for field `v`, with record `t:5`, but expected a int"

    def reject(batch: int) -> Any:
        return [{"status": "ERR", "result": message, "time": ""}] if batch == 3 else None

    conn = FakeConnection(importer(fail=reject))
    with pytest.raises(DataImportError) as info:
        await conn.import_jsonl("t", [line(i) for i in range(1, 8)], batch_size=2, relation=False)

    error = info.value
    assert str(error) == f"Batch at lines 5-6 was rejected by t: {message}"
    assert error.errors == [message]
    assert error.code is None
    assert error.report == ImportReport(table="t", lines=6, rows=4, batches=2)
    assert len(conn.calls) == 3  # nothing sent after the rejected batch


@pytest.mark.parametrize(
    "error",
    [
        QueryError("HTTP 400: Parse error", code=400),
        ConnectionError("Request failed: Connection reset"),
        TimeoutError("Request timed out after 30.0s"),
    ],
    ids=["http-400", "connection", "timeout"],
)
async def test_import_request_failure_on_batch_three_is_e8b(error: Exception) -> None:
    def fail(batch: int) -> Any:
        if batch == 3:
            raise error
        return None

    conn = FakeConnection(importer(fail=fail))
    logged: list[str] = []
    with pytest.raises(DataImportError) as info:
        await conn.import_jsonl(
            "t",
            [line(i) for i in range(1, 9)],
            batch_size=2,
            relation=False,
            _on_query=lambda s, v, ms: logged.append(s),
        )

    wrapped = info.value
    assert str(wrapped) == (
        f"Batch at lines 5-6 could not be sent to t ({type(error).__name__}: {error}); "
        "the batches before it are written, this one may or may not be"
    )
    assert wrapped.report == ImportReport(table="t", lines=6, rows=4, batches=2)
    assert wrapped.__cause__ is error
    assert wrapped.code == getattr(error, "code", None)
    assert wrapped.errors == [str(error)]
    assert len(logged) == 2  # not called for the batch whose request raised


async def test_import_cancellation_propagates_unchanged() -> None:
    cancelled = asyncio.CancelledError()

    def fail(batch: int) -> Any:
        raise cancelled

    conn = FakeConnection(importer(fail=fail))
    with pytest.raises(asyncio.CancelledError) as info:
        await conn.import_jsonl("t", [line(1)], relation=False)
    assert info.value is cancelled


async def test_import_empty_source_sends_nothing() -> None:
    conn = FakeConnection(importer())
    report = await conn.import_jsonl("t", ["", "  "])
    assert report == ImportReport(table="t", lines=0, rows=0, batches=0)
    assert conn.calls == []


# relation=None: C10 once, before the first batch (M47)


async def test_relation_auto_reads_the_table_type_once_before_the_first_batch() -> None:
    conn = FakeConnection(importer("DEFINE TABLE t TYPE RELATION IN p OUT p SCHEMALESS PERMISSIONS NONE"))

    await conn.import_jsonl("t", [edge(i) for i in range(1, 6)], batch_size=2)

    assert [sql for sql, _ in conn.calls] == [C10, C5, C5, C5]
    assert conn.calls[0][1] == {"_x_table": "t"}


async def test_relation_auto_c10_is_passed_to_on_query() -> None:
    conn = FakeConnection(importer("DEFINE TABLE t TYPE NORMAL SCHEMALESS PERMISSIONS NONE"))
    logged: list[tuple[str, dict[str, Any]]] = []
    await conn.import_jsonl("t", [line(1)], _on_query=lambda s, v, ms: logged.append((s, v)))
    assert logged[0] == (C10, {"_x_table": "t"})
    assert logged[1][0] == C3


async def test_relation_auto_type_relation_uses_c5_for_rows_without_in_out() -> None:
    conn = FakeConnection(importer("DEFINE TABLE t TYPE RELATION SCHEMALESS PERMISSIONS NONE"))
    await conn.import_jsonl("t", [line(1), edge(2)], batch_size=1)
    assert [sql for sql, _ in inserts(conn)] == [C5, C5]


async def test_relation_auto_type_normal_inserts_plain_rows_holding_in_and_out() -> None:
    conn = FakeConnection(importer("DEFINE TABLE OVERWRITE t TYPE NORMAL SCHEMAFULL PERMISSIONS NONE"))
    report = await conn.import_jsonl("t", [edge(1), line(2), edge(3)], batch_size=10)
    assert [sql for sql, _ in inserts(conn)] == [C3]
    assert report.rows == 3


@pytest.mark.parametrize(
    "table_info",
    [
        "DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS NONE",
        None,
        "something else",
        [{"status": "ERR", "result": "IAM error: Not enough permissions to perform this action", "time": ""}],
    ],
    ids=["type-any", "none", "no-match", "err"],
)
async def test_relation_auto_falls_back_to_per_batch_detection(table_info: Any) -> None:
    conn = FakeConnection(importer(table_info))

    await conn.import_jsonl("t", [edge(1), edge(2), line(3), line(4)], batch_size=2)

    assert [sql for sql, _ in conn.calls] == [C10, C5, C3]


async def test_relation_auto_mixed_batch_is_e7() -> None:
    conn = FakeConnection(importer("DEFINE TABLE t TYPE ANY SCHEMALESS PERMISSIONS NONE"))
    with pytest.raises(DataFormatError) as info:
        await conn.import_jsonl("t", [line(1), line(2), edge(3), "", line(4)], batch_size=2)
    assert str(info.value) == (
        "Lines 3-5: some rows have record 'in' and 'out' fields and some do not; pass relation=True or relation=False"
    )
    assert [sql for sql, _ in conn.calls] == [C10, C3]


async def test_relation_auto_needs_record_ids_not_strings() -> None:
    conn = FakeConnection(importer(None))
    await conn.import_jsonl("t", ['{"id": 1, "in": "p:1", "out": "p:2"}'])
    assert [sql for sql, _ in inserts(conn)] == [C3]


@pytest.mark.parametrize("relation", [True, False])
async def test_explicit_relation_sends_no_c10(relation: bool) -> None:
    conn = FakeConnection(importer())
    await conn.import_jsonl("t", [line(1), edge(2)], relation=relation)
    assert [sql for sql, _ in conn.calls] == [C5 if relation else C3]


# Argument checks (before any I/O)


async def test_import_on_conflict_e6() -> None:
    conn = FakeConnection(importer())
    with pytest.raises(ValueError, match=r"^on_conflict must be 'error' or 'ignore', got 'update'$"):
        await conn.import_jsonl("t", [line(1)], on_conflict="update")  # type: ignore[arg-type]
    assert conn.calls == []


@pytest.mark.parametrize(
    ("kwargs", "exc", "text"),
    [
        ({"batch_size": 0}, ValueError, "batch_size must be >= 1, got 0"),
        ({"batch_size": True}, TypeError, "batch_size must be an int, got bool"),
        ({"max_batch_bytes": 0}, ValueError, "max_batch_bytes must be >= 1, got 0"),
        ({"max_batch_bytes": 1.5}, TypeError, "max_batch_bytes must be an int, got float"),
    ],
)
async def test_import_int_arguments_e1(kwargs: dict[str, Any], exc: type[Exception], text: str) -> None:
    conn = FakeConnection(importer())
    with pytest.raises(exc, match=f"^{text}$"):
        await conn.import_jsonl("t", [line(1)], **kwargs)


@pytest.mark.parametrize("table", ["t y", "t\n", "", 5])
async def test_import_invalid_table_e5(table: Any) -> None:
    conn = FakeConnection(importer())
    with pytest.raises(ValueError, match=r"^Invalid table name: "):
        await conn.import_jsonl(table, [line(1)])


async def test_import_json_protocol_is_refused_e4() -> None:
    conn = FakeConnection(importer(), protocol="json")
    with pytest.raises(ValueError, match=r"^import_jsonl\(\) needs the CBOR protocol: "):
        await conn.import_jsonl("t", [line(1)])
    assert conn.calls == []


async def test_import_argument_errors_do_not_open_the_source(tmp_path: Path) -> None:
    conn = FakeConnection(importer())
    with pytest.raises(ValueError):
        await conn.import_jsonl("t y", tmp_path / "missing.jsonl")


# Sources


async def test_import_from_a_path_reads_off_the_event_loop(tmp_path: Path, thread_spy: list[str]) -> None:
    path = tmp_path / "in.jsonl"
    path.write_text("".join(line(i) + "\n" for i in range(1, 2501)) + "\n", encoding="utf-8")
    conn = FakeConnection(importer())

    report = await conn.import_jsonl("t", path, relation=False)

    assert report == ImportReport(table="t", lines=2500, rows=2500, batches=3)
    # open, three blocks of up to 1,000 lines, the empty read that ends the file, close
    assert len(thread_spy) >= 4
    assert all(name != "write" for name in thread_spy)


async def test_import_from_a_str_path(tmp_path: Path) -> None:
    path = tmp_path / "in.jsonl"
    path.write_text(f"﻿{line(1)}\r\n{line(2)}\n", encoding="utf-8")
    conn = FakeConnection(importer())
    report = await conn.import_jsonl("t", str(path), relation=False)
    assert report.rows == 2
    assert [row["v"] for row in conn.calls[0][1]["_x_rows"]] == [1, 2]


async def test_import_from_a_text_stream_reads_off_the_event_loop(thread_spy: list[str]) -> None:
    stream = io.StringIO("".join(line(i) + "\n" for i in range(1, 4)))
    conn = FakeConnection(importer())
    report = await conn.import_jsonl("t", stream, relation=False)
    assert report.rows == 3
    assert thread_spy  # read in a worker thread
    assert not stream.closed


async def test_import_from_an_async_generator() -> None:
    async def produce() -> AsyncIterator[str]:
        for i in range(1, 4):
            yield line(i) + "\n"

    conn = FakeConnection(importer())
    report = await conn.import_jsonl("t", produce(), batch_size=2, relation=False)
    assert report == ImportReport(table="t", lines=3, rows=3, batches=2)


async def test_import_from_a_missing_path_raises_os_error(tmp_path: Path) -> None:
    conn = FakeConnection(importer())
    with pytest.raises(FileNotFoundError):
        await conn.import_jsonl("t", tmp_path / "missing.jsonl", relation=False)


@pytest.mark.parametrize("relation", [True, False])
async def test_import_rewrites_the_id_table_to_the_target(relation: bool) -> None:
    # INSERT RELATION refuses an id of another table ("Found e:1 for the `id` field, but a
    # specific record has been specified"); plain INSERT rewrites it on the server (M21).
    conn = FakeConnection(importer())
    key = [1, "a"]
    source = [
        dumps_record({"id": RecordId("e", key), "in": RecordId("p", 1), "out": RecordId("p", 2)}),
        dumps_record({"id": RecordId("t", 2), "in": RecordId("p", 1), "out": RecordId("p", 2)}),
        dumps_record({"id": "plain", "in": RecordId("p", 1), "out": RecordId("p", 2)}),
    ]

    await conn.import_jsonl("t", source, relation=relation)

    ids = [row["id"] for row in conn.calls[0][1]["_x_rows"]]
    assert ids == [RecordId("t", key), RecordId("t", 2), "plain"]


# ---------------------------------------------------------------------------
# export_database_stream() / export_database() (U8)
# ---------------------------------------------------------------------------

DUMP_CHUNKS = [b"OPTION IMPORT;\n", b"DEFINE TABLE t TYPE NORMAL SCHEMALESS PERMISSIONS NONE;\n", b"INSERT [{ id: t:1 }];\n"]
DEFAULT_BODY = {
    "users": True,
    "accesses": True,
    "params": True,
    "functions": True,
    "analyzers": True,
    "tables": True,
    "records": True,
}

Handler = Callable[[httpx.Request], Any]


async def _chunks(parts: list[bytes], error: Exception | None = None) -> AsyncIterator[bytes]:
    for part in parts:
        yield part
    if error is not None:
        raise error


def dump_handler(
    requests: list[httpx.Request], *, status: int = 200, text: str = "", error: Exception | None = None
) -> Handler:
    """An ``/export`` endpoint answering ``DUMP_CHUNKS`` streamed (or ``status`` with ``text``)."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if status != 200:
            return httpx.Response(status, text=text)
        return httpx.Response(200, content=_chunks(DUMP_CHUNKS, error))

    return handler


@pytest.fixture
async def http_conn() -> AsyncIterator[Callable[[Handler], HTTPConnection]]:
    """Build an ``HTTPConnection`` whose client answers through a ``MockTransport`` handler."""
    clients: list[httpx.AsyncClient] = []

    def build(handler: Handler) -> HTTPConnection:
        conn = HTTPConnection("http://fake", "ns", "db")
        conn._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://fake")
        clients.append(conn._client)
        return conn

    yield build
    for client in clients:
        await client.aclose()


async def test_export_stream_posts_c8_with_the_full_default_body(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(dump_handler(requests))
    conn._token = "tok"

    async with conn.export_database_stream() as chunks:
        received = [chunk async for chunk in chunks]

    assert received == DUMP_CHUNKS
    (request,) = requests
    assert request.method == "POST" and request.url.path == "/export"
    assert json.loads(request.content) == DEFAULT_BODY
    assert request.headers["Surreal-NS"] == "ns"
    assert request.headers["Surreal-DB"] == "db"
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Accept"] == "application/json"
    # D20: no client timeout on the transfer (the connection's 30 s would cut a large dump)
    assert request.extensions["timeout"] == {"connect": None, "read": None, "write": None, "pool": None}


async def test_export_stream_sends_no_authorization_without_a_token(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(dump_handler(requests))

    async with conn.export_database_stream() as chunks:
        assert [chunk async for chunk in chunks] == DUMP_CHUNKS

    assert "Authorization" not in requests[0].headers


@pytest.mark.parametrize("option", ["users", "accesses", "params", "functions", "analyzers", "records"])
async def test_export_stream_sends_each_option(http_conn: Callable[[Handler], HTTPConnection], option: str) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(dump_handler(requests))

    async with conn.export_database_stream(**{option: False}) as chunks:
        async for _ in chunks:
            pass

    assert json.loads(requests[0].content) == {**DEFAULT_BODY, option: False}


async def test_export_stream_sends_the_table_list(http_conn: Callable[[Handler], HTTPConnection]) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(dump_handler(requests))

    async with conn.export_database_stream(tables=("person", "post")) as chunks:
        async for _ in chunks:
            pass

    assert json.loads(requests[0].content)["tables"] == ["person", "post"]


@pytest.mark.parametrize("table", ["bad-name", "t\n", "", 1, "a.b"])
def test_export_stream_validates_tables_e5_before_any_request(table: Any) -> None:
    conn = HTTPConnection("http://fake", "ns", "db")  # not connected: E5 comes first, at the call
    with pytest.raises(ValueError, match=r"^Invalid table name: .* \(expected an identifier\)$"):
        conn.export_database_stream(tables=["ok", table])


def test_export_stream_refuses_a_single_string_for_tables() -> None:
    conn = HTTPConnection("http://fake", "ns", "db")
    with pytest.raises(TypeError, match="tables must be a sequence of table names, not a str"):
        conn.export_database_stream(tables="person")


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_export_stream_non_200_is_e11_with_the_code(http_conn: Callable[[Handler], HTTPConnection], status: int) -> None:
    conn = http_conn(dump_handler([], status=status, text="Specify a namespace to use"))

    with pytest.raises(QueryError) as caught:
        async with conn.export_database_stream():
            pytest.fail("the body must not run on an error status")

    assert str(caught.value) == f"Export failed: HTTP {status} - Specify a namespace to use"
    assert caught.value.code == status
    assert conn._inflight_rpcs == 0


async def test_export_stream_counts_in_flight_for_the_whole_transfer(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    conn = http_conn(dump_handler([]))
    assert conn._inflight_rpcs == 0

    async with conn.export_database_stream() as chunks:
        assert conn._inflight_rpcs == 1
        async for _ in chunks:
            assert conn._inflight_rpcs == 1
        assert conn._inflight_rpcs == 1

    assert conn._inflight_rpcs == 0
    assert conn._released.is_set()


async def test_export_stream_releases_when_the_body_raises(http_conn: Callable[[Handler], HTTPConnection]) -> None:
    conn = http_conn(dump_handler([]))

    with pytest.raises(RuntimeError, match="sink failed"):
        async with conn.export_database_stream() as chunks:
            async for _ in chunks:
                raise RuntimeError("sink failed")

    assert conn._inflight_rpcs == 0


async def test_export_stream_closes_a_connection_marked_close_when_idle(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    conn = http_conn(dump_handler([]))

    async with conn.export_database_stream() as chunks:
        await conn._close_when_idle()  # a cancelled reconnect() drain: the transfer is in use
        assert conn._client is not None
        assert [chunk async for chunk in chunks] == DUMP_CHUNKS

    assert conn._client is None  # the transfer was the last use: it closed the connection


async def test_export_stream_read_error_mid_stream_is_connection_error(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    cause = httpx.ReadError("peer closed")
    conn = http_conn(dump_handler([], error=cause))
    received: list[bytes] = []

    with pytest.raises(ConnectionError) as caught:
        async with conn.export_database_stream() as chunks:
            async for chunk in chunks:
                received.append(chunk)

    assert received == DUMP_CHUNKS
    assert str(caught.value) == f"Export failed: {cause!r}"
    assert caught.value.__cause__ is cause
    assert conn._inflight_rpcs == 0


async def test_export_stream_refused_send_is_connection_error(http_conn: Callable[[Handler], HTTPConnection]) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    conn = http_conn(refuse)

    with pytest.raises(ConnectionError, match=r"^Export failed: ConnectError\('refused'\)$") as caught:
        async with conn.export_database_stream():
            pass

    assert isinstance(caught.value.__cause__, httpx.ConnectError)
    assert conn._inflight_rpcs == 0


async def test_export_not_connected_is_e13() -> None:
    conn = HTTPConnection("http://fake", "ns", "db")

    with pytest.raises(ConnectionError, match=r"^Not connected\. Call connect\(\) first\.$"):
        async with conn.export_database_stream():
            pass
    with pytest.raises(ConnectionError, match=r"^Not connected\. Call connect\(\) first\.$"):
        await conn.export_database(io.BytesIO())
    assert conn._inflight_rpcs == 0


async def test_export_database_writes_every_chunk_and_returns_the_byte_count(
    http_conn: Callable[[Handler], HTTPConnection], tmp_path: Path, thread_spy: list[str]
) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(dump_handler(requests))
    dest = tmp_path / "db.surql"

    written = await conn.export_database(dest, tables=["t"], users=False, accesses=False)

    assert written == sum(len(chunk) for chunk in DUMP_CHUNKS)
    assert dest.read_bytes() == b"".join(DUMP_CHUNKS)
    assert json.loads(requests[0].content) == {**DEFAULT_BODY, "tables": ["t"], "users": False, "accesses": False}
    # open, one write per chunk, close: all off the event loop
    assert len(thread_spy) == len(DUMP_CHUNKS) + 2
    assert conn._inflight_rpcs == 0


async def test_export_database_to_a_binary_stream_leaves_it_open(
    http_conn: Callable[[Handler], HTTPConnection], thread_spy: list[str]
) -> None:
    conn = http_conn(dump_handler([]))
    sink = io.BytesIO()

    written = await conn.export_database(sink)

    assert written == len(sink.getvalue())
    assert sink.getvalue() == b"".join(DUMP_CHUNKS)
    assert not sink.closed
    assert len(thread_spy) == len(DUMP_CHUNKS)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
@pytest.mark.usefixtures("umask_022")
async def test_export_database_creates_a_new_file_owner_only(
    http_conn: Callable[[Handler], HTTPConnection], tmp_path: Path
) -> None:
    conn = http_conn(dump_handler([]))
    dest = tmp_path / "new.surql"

    await conn.export_database(str(dest))

    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
async def test_export_database_keeps_the_mode_of_an_existing_file(
    http_conn: Callable[[Handler], HTTPConnection], tmp_path: Path
) -> None:
    conn = http_conn(dump_handler([]))
    dest = tmp_path / "existing.surql"
    dest.write_bytes(b"an older and much longer dump " * 10)
    dest.chmod(0o644)

    await conn.export_database(dest)

    assert stat.S_IMODE(dest.stat().st_mode) == 0o644
    assert dest.read_bytes() == b"".join(DUMP_CHUNKS)  # truncated, then written


async def test_export_database_error_status_leaves_an_existing_file_untouched(
    http_conn: Callable[[Handler], HTTPConnection], tmp_path: Path
) -> None:
    conn = http_conn(dump_handler([], status=403, text="Not enough permissions"))
    dest = tmp_path / "keep.surql"
    dest.write_bytes(b"previous dump")

    with pytest.raises(QueryError, match=r"^Export failed: HTTP 403 - Not enough permissions$"):
        await conn.export_database(dest)

    assert dest.read_bytes() == b"previous dump"
    assert conn._inflight_rpcs == 0


async def test_export_database_read_error_leaves_the_partial_file(
    http_conn: Callable[[Handler], HTTPConnection], tmp_path: Path
) -> None:
    cause = httpx.ReadError("peer closed")
    conn = http_conn(dump_handler([], error=cause))
    dest = tmp_path / "partial.surql"

    with pytest.raises(ConnectionError) as caught:
        await conn.export_database(dest)

    assert caught.value.__cause__ is cause
    assert dest.read_bytes() == b"".join(DUMP_CHUNKS)
    assert conn._inflight_rpcs == 0


async def test_export_database_validates_before_opening_the_destination(tmp_path: Path) -> None:
    conn = HTTPConnection("http://fake", "ns", "db")
    dest = tmp_path / "never.surql"

    with pytest.raises(ValueError, match="Invalid table name"):
        await conn.export_database(dest, tables=["bad name"])

    assert not dest.exists()


async def test_export_stream_body_may_stop_iterating_early(http_conn: Callable[[Handler], HTTPConnection]) -> None:
    conn = http_conn(dump_handler([]))

    async with conn.export_database_stream() as chunks:
        async for chunk in chunks:
            assert chunk == DUMP_CHUNKS[0]
            break

    assert conn._inflight_rpcs == 0


# ---------------------------------------------------------------------------
# import_database() (U9)
# ---------------------------------------------------------------------------

DUMP = b"OPTION IMPORT;\nDEFINE TABLE t TYPE NORMAL SCHEMALESS PERMISSIONS NONE;\n" + b"INSERT [{ id: t:1 }];\n" * 9000
ALREADY = "Database record `t:1` already exists"
ERR_LIST = [
    {"kind": "AlreadyExists", "result": ALREADY, "status": "ERR", "time": "1µs", "type": None},
    {"kind": "NotFound", "result": "The table 'u' does not exist", "status": "ERR", "time": "1µs", "type": None},
]
FORBIDDEN = {
    "code": 403,
    "details": "Forbidden",
    "description": "Not allowed to do this.",
    "information": "IAM error: Not enough permissions to perform this action",
}


def import_handler(
    requests: list[httpx.Request],
    *,
    status: int = 200,
    payload: Any = (),
    text: str | None = None,
    seen: list[Any] | None = None,
    conn: list[HTTPConnection] | None = None,
) -> Handler:
    """An ``/import`` endpoint: reads the streamed body, answers ``status`` with ``payload`` (JSON) or ``text``."""

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if seen is not None and conn:
            seen.append(conn[0]._inflight_rpcs)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=list(payload) if isinstance(payload, tuple) else payload)

    return handler


async def _agen(parts: list[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


class Counting(io.BytesIO):
    """A binary stream recording the size of each ``read()``."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.sizes: list[int] = []

    def read(self, size: int | None = -1, /) -> bytes:
        self.sizes.append(-1 if size is None else size)
        return super().read(size)


async def test_import_posts_c9_streamed_and_returns_none_on_200_empty_list(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    requests: list[httpx.Request] = []
    holder: list[HTTPConnection] = []
    seen: list[Any] = []
    conn = http_conn(import_handler(requests, seen=seen, conn=holder))
    holder.append(conn)
    conn._token = "tok"

    result = await conn.import_database(DUMP)  # type: ignore[func-returns-value]

    assert result is None
    (request,) = requests
    assert request.method == "POST" and request.url.path == "/import"
    assert request.content == DUMP
    assert request.headers["Surreal-NS"] == "ns"
    assert request.headers["Surreal-DB"] == "db"
    assert request.headers["Authorization"] == "Bearer tok"
    assert request.headers["Accept"] == "application/json"
    assert "Content-Type" not in request.headers
    # streamed: chunked, never a Content-Length holding the whole dump in one body
    assert request.headers["Transfer-Encoding"] == "chunked"
    assert "Content-Length" not in request.headers
    # D20: no client timeout on the transfer
    assert request.extensions["timeout"] == {"connect": None, "read": None, "write": None, "pool": None}
    assert seen == [1]
    assert conn._inflight_rpcs == 0
    assert conn._released.is_set()


async def test_import_sends_no_authorization_without_a_token(http_conn: Callable[[Handler], HTTPConnection]) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(import_handler(requests))

    await conn.import_database(b"OPTION IMPORT;")

    assert "Authorization" not in requests[0].headers


async def test_import_from_a_path_reads_64_kib_chunks_off_the_event_loop(
    http_conn: Callable[[Handler], HTTPConnection], tmp_path: Path, thread_spy: list[str]
) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(import_handler(requests))
    path = tmp_path / "db.surql"
    path.write_bytes(DUMP)

    await conn.import_database(path)
    await conn.import_database(str(path))

    assert [r.content for r in requests] == [DUMP, DUMP]
    reads = -(-len(DUMP) // (64 * 1024)) + 1  # every chunk, then the empty read at the end
    # open, each read, close: all in a worker thread
    assert thread_spy == (["open"] + ["read"] * reads + ["close"]) * 2


async def test_import_from_a_binary_stream_reads_64_kib_chunks_and_leaves_it_open(
    http_conn: Callable[[Handler], HTTPConnection], thread_spy: list[str]
) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(import_handler(requests))
    stream = Counting(DUMP)

    await conn.import_database(stream)

    assert requests[0].content == DUMP
    assert set(stream.sizes) == {64 * 1024}
    assert len(thread_spy) == len(stream.sizes)
    assert not stream.closed


async def test_import_from_an_async_iterable_streams_its_chunks(http_conn: Callable[[Handler], HTTPConnection]) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(import_handler(requests))
    parts = [b"OPTION IMPORT;\n", b"DEFINE TABLE t;\n", b"INSERT [{ id: t:1 }];\n"]

    await conn.import_database(_agen(parts))

    assert requests[0].content == b"".join(parts)


async def test_import_slices_bytes_in_64_kib_chunks() -> None:
    from surreal_sdk.connection.http import _slice_chunks

    sizes = [len(chunk) async for chunk in _slice_chunks(DUMP)]

    assert sum(sizes) == len(DUMP)
    assert sizes[:-1] == [64 * 1024] * (len(sizes) - 1)
    assert 0 < sizes[-1] <= 64 * 1024


@pytest.mark.parametrize("status", [200, 422])
async def test_import_statement_errors_are_e12a_whatever_the_status(
    http_conn: Callable[[Handler], HTTPConnection], status: int
) -> None:
    conn = http_conn(import_handler([], status=status, payload=ERR_LIST))

    with pytest.raises(DataImportError) as caught:
        await conn.import_database(DUMP)

    error = caught.value
    assert str(error) == f"Import failed: 2 statement(s) rejected; first: {ALREADY}"
    assert error.errors == [ALREADY, "The table 'u' does not exist"]
    assert error.code == status
    assert error.report is None
    assert conn._inflight_rpcs == 0


async def test_import_other_status_is_e12b_with_the_server_information(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    conn = http_conn(import_handler([], status=403, payload=FORBIDDEN))

    with pytest.raises(DataImportError) as caught:
        await conn.import_database(DUMP)

    information = FORBIDDEN["information"]
    assert str(caught.value) == f"Import failed: HTTP 403 - {information}"
    assert caught.value.errors == [information]
    assert caught.value.code == 403
    assert caught.value.report is None
    assert conn._inflight_rpcs == 0


async def test_import_other_status_without_json_is_e12b_with_the_text(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    conn = http_conn(import_handler([], status=502, text="Bad gateway"))

    with pytest.raises(DataImportError, match=r"^Import failed: HTTP 502 - Bad gateway$") as caught:
        await conn.import_database(DUMP)

    assert caught.value.errors == ["Bad gateway"]
    assert caught.value.code == 502


@pytest.mark.parametrize(
    "cause",
    [httpx.ReadError(""), httpx.WriteError("broken pipe"), httpx.RemoteProtocolError("server disconnected")],
    ids=["read", "write", "protocol"],
)
async def test_import_transport_error_is_e12c(http_conn: Callable[[Handler], HTTPConnection], cause: Exception) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise cause

    conn = http_conn(fail)

    with pytest.raises(DataImportError) as caught:
        await conn.import_database(DUMP)

    error = caught.value
    assert str(error) == (
        f"Import failed: the connection was closed during the upload ({cause!r}); the server stops reading a body "
        "it rejects up front: one that does not start with OPTION IMPORT;, a user without import rights, or a "
        "missing database"
    )
    assert error.errors == [repr(cause)]
    assert error.code is None
    assert error.report is None
    assert error.__cause__ is cause
    assert conn._inflight_rpcs == 0


async def test_import_transport_error_closes_the_opened_file(
    http_conn: Callable[[Handler], HTTPConnection], tmp_path: Path, thread_spy: list[str]
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("")

    conn = http_conn(fail)
    path = tmp_path / "db.surql"
    path.write_bytes(DUMP)

    with pytest.raises(DataImportError):
        await conn.import_database(path)

    assert thread_spy[0] == "open" and thread_spy[-1] == "close"


async def test_import_closes_a_connection_marked_close_when_idle(
    http_conn: Callable[[Handler], HTTPConnection],
) -> None:
    holder: list[HTTPConnection] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await holder[0]._close_when_idle()  # a cancelled reconnect() drain: the upload is in use
        assert holder[0]._client is not None
        return httpx.Response(200, json=[])

    conn = http_conn(handler)
    holder.append(conn)

    await conn.import_database(DUMP)

    assert conn._client is None  # the upload was the last use: it closed the connection


async def test_import_not_connected_is_e13(tmp_path: Path) -> None:
    conn = HTTPConnection("http://fake", "ns", "db")

    with pytest.raises(ConnectionError, match=r"^Not connected\. Call connect\(\) first\.$"):
        await conn.import_database(DUMP)
    assert conn._inflight_rpcs == 0


@pytest.mark.parametrize("source", [42, None, [b"OPTION IMPORT;"], {"a": 1}])
async def test_import_refuses_an_unsupported_source_before_any_request(
    http_conn: Callable[[Handler], HTTPConnection], source: Any
) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(import_handler(requests))

    with pytest.raises(TypeError, match="source must be a path, bytes, a binary stream or an async iterable of bytes"):
        await conn.import_database(source)

    assert requests == []
    assert conn._inflight_rpcs == 0


async def test_import_missing_path_raises_os_error_before_any_request(
    http_conn: Callable[[Handler], HTTPConnection], tmp_path: Path
) -> None:
    requests: list[httpx.Request] = []
    conn = http_conn(import_handler(requests))

    with pytest.raises(FileNotFoundError):
        await conn.import_database(tmp_path / "missing.surql")

    assert requests == []
    assert conn._inflight_rpcs == 0


async def test_import_report_lines_include_a_line_decoded_before_a_failed_size_flush() -> None:
    """The line that triggers a byte-size flush is decoded first, so it is counted."""

    def reject(batch: int) -> Any:
        return [{"status": "ERR", "result": "boom", "time": ""}] if batch == 1 else None

    conn = FakeConnection(importer(fail=reject))
    source = [line(1), line(2)]
    with pytest.raises(DataImportError) as info:
        await conn.import_jsonl("t", source, max_batch_bytes=len(source[0]) + 1, relation=False)

    assert str(info.value) == "Batch at lines 1-1 was rejected by t: boom"
    assert info.value.report == ImportReport(table="t", lines=2, rows=0, batches=0)
