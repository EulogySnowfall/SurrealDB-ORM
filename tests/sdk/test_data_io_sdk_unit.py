"""Unit tests for the SDK's JSON Lines batch I/O (``iter_records()``, ``export_jsonl()``), U6.

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
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Self

import pytest

from surreal_sdk.connection.base import BaseSurrealConnection
from surreal_sdk.exceptions import ConnectionError, DataFormatError, QueryError, TableNotFoundError, TimeoutError
from surreal_sdk.jsonl import _iter_batches, loads_record
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
