"""Unit tests for #258 (v2): four SDK defects left open after 0.21.14.

1. Every live-query builder wrote ``DIFF`` after the ``WHERE`` clause
   (``LIVE SELECT * FROM t WHERE ... DIFF``), which SurrealDB rejects; the
   statement is ``LIVE SELECT DIFF FROM t [WHERE ...]``. The five builders go
   through :func:`surreal_sdk.protocol.rpc.live_select_query`.
2. An ``int`` record id outside the signed 64-bit range has no literal: the
   server reads ``t:9223372036854775808`` as a *string* id.
3. A naive ``datetime`` bound as a parameter made cbor2 raise; it is read as
   UTC now, with a ``RuntimeWarning``, the way ``live()`` already wrote it.
4. Over WebSocket an encoding error came back as ``ConnectionError``.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import cbor2
import pytest

from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.exceptions import ConnectionError as SurrealConnectionError
from surreal_sdk.protocol.cbor import RecordId, decode, encode
from surreal_sdk.protocol.rpc import RPCRequest, live_select_query
from surreal_sdk.streaming.live_query import LiveQuery
from surreal_sdk.streaming.live_select import LiveSelectStream, LiveSubscriptionParams
from surreal_sdk.types import QueryResponse


def _recording_connection() -> tuple[WebSocketConnection, list[str]]:
    """A WebSocket connection whose ``query()`` records the text it is given."""
    sent: list[str] = []

    async def fake_query(sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        sent.append(sql)
        return QueryResponse.from_rpc_result([{"status": "OK", "result": "live-id", "time": "1ms"}])

    conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
    conn.query = fake_query  # type: ignore[method-assign]
    return conn, sent


async def _noop(_: Any) -> None:
    return None


# ==================== 1. LIVE SELECT DIFF ====================


class TestLiveSelectQuery:
    """The one place a ``LIVE SELECT`` statement is written."""

    def test_plain(self) -> None:
        assert live_select_query("t") == "LIVE SELECT * FROM t"

    def test_where(self) -> None:
        assert live_select_query("t", "x = 1") == "LIVE SELECT * FROM t WHERE x = 1"

    def test_diff_replaces_the_projection(self) -> None:
        assert live_select_query("t", diff=True) == "LIVE SELECT DIFF FROM t"

    def test_diff_with_where(self) -> None:
        assert live_select_query("t", "x = 1", diff=True) == "LIVE SELECT DIFF FROM t WHERE x = 1"

    def test_empty_where_is_no_where(self) -> None:
        assert live_select_query("t", "", diff=True) == "LIVE SELECT DIFF FROM t"


class TestEveryBuilderPutsDiffBeforeFrom:
    """The five builders send the same statement for ``diff=True``."""

    def test_rpc_request_live(self) -> None:
        assert RPCRequest.live("t", diff=True).params[0] == "LIVE SELECT DIFF FROM t"

    async def test_live_select_stream_start(self) -> None:
        conn, sent = _recording_connection()
        await LiveSelectStream(conn, "t", where="x = $v", params={"v": 1}, diff=True).start()
        assert sent == ["LIVE SELECT DIFF FROM t WHERE x = 1"]

    async def test_resubscribe_one(self) -> None:
        conn, sent = _recording_connection()
        await conn._resubscribe_one(
            LiveSubscriptionParams(table="t", where="x = $v", params={"v": 1}, diff=True, callback=None, on_reconnect=None)
        )
        assert sent == ["LIVE SELECT DIFF FROM t WHERE x = 1"]

    async def test_websocket_live(self) -> None:
        conn, sent = _recording_connection()
        await conn.live("t", _noop, diff=True)
        assert sent == ["LIVE SELECT DIFF FROM t"]

    async def test_live_query_subscribe(self) -> None:
        conn, sent = _recording_connection()
        await LiveQuery(conn, "t", where="x = 1", diff=True).subscribe(_noop)
        assert sent == ["LIVE SELECT DIFF FROM t WHERE x = 1"]

    async def test_without_diff_nothing_changes(self) -> None:
        conn, sent = _recording_connection()
        await LiveSelectStream(conn, "t", where="x = $v", params={"v": 1}).start()
        await LiveQuery(conn, "t", where="x = 1").subscribe(_noop)
        await conn.live("t", _noop)
        assert sent == ["LIVE SELECT * FROM t WHERE x = 1", "LIVE SELECT * FROM t WHERE x = 1", "LIVE SELECT * FROM t"]
        assert RPCRequest.live("t").params[0] == "LIVE SELECT * FROM t"


# ==================== 2. int record ids outside i64 ====================


class TestRecordIdIntegerRange:
    """A literal is written only for an ``int`` id SurrealDB reads as that number."""

    @pytest.mark.parametrize("record_id", [2**63, -(2**63) - 1, 10**30, -(10**30)])
    def test_out_of_range_raises(self, record_id: int) -> None:
        with pytest.raises(TypeError, match="signed 64-bit"):
            LiveSelectStream._format_value(RecordId("t", record_id))

    @pytest.mark.parametrize(
        ("record_id", "literal"),
        [(2**63 - 1, "`t`:9223372036854775807"), (-(2**63), "`t`:-9223372036854775808"), (0, "`t`:0")],
    )
    def test_edges_still_render(self, record_id: int, literal: str) -> None:
        assert LiveSelectStream._format_value(RecordId("t", record_id)) == literal

    def test_out_of_range_inside_a_filter_raises(self) -> None:
        with pytest.raises(TypeError, match="signed 64-bit"):
            LiveSelectStream._inline_params_static("ref = $r", {"r": [RecordId("t", 2**63)]})


# ==================== 3. naive datetime ====================


class TestNaiveDatetimeIsUtc:
    """A naive ``datetime`` is read as UTC by the encoder, as ``live()`` reads it."""

    def test_encodes_as_the_utc_instant(self) -> None:
        with pytest.warns(RuntimeWarning, match="naive datetime"):
            naive = encode({"d": datetime(2026, 2, 1, 12, 30)})
        assert naive == encode({"d": datetime(2026, 2, 1, 12, 30, tzinfo=UTC)})
        assert decode(naive) == {"d": datetime(2026, 2, 1, 12, 30, tzinfo=UTC)}

    def test_nested_naive_value_is_encoded_too(self) -> None:
        with pytest.warns(RuntimeWarning, match="naive datetime"):
            data = encode(["x", {"at": [datetime(2026, 2, 1)]}])
        assert decode(data) == ["x", {"at": [datetime(2026, 2, 1, tzinfo=UTC)]}]

    def test_aware_value_is_unchanged_and_not_warned(self) -> None:
        aware = datetime(2026, 2, 1, 12, 30, tzinfo=UTC)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            data = encode({"d": aware})
        assert data == cbor2.dumps({"d": aware})

    def test_rpc_request_carries_a_naive_value(self) -> None:
        with pytest.warns(RuntimeWarning, match="naive datetime"):
            payload = RPCRequest(method="query", params=["RETURN $d", {"d": datetime(2026, 2, 1)}]).to_cbor()
        assert decode(payload)["params"][1]["d"] == datetime(2026, 2, 1, tzinfo=UTC)

    def test_live_filter_renders_the_same_instant_and_warns(self) -> None:
        with pytest.warns(RuntimeWarning, match="naive datetime"):
            assert LiveSelectStream._format_value(datetime(2026, 2, 1, 12, 30)) == "d'2026-02-01T12:30:00+00:00'"


# ==================== 4. WebSocket encoding errors ====================


def _connected(protocol: str) -> tuple[WebSocketConnection, MagicMock]:
    """A connection marked connected over a fake socket that records what it sends."""
    conn = WebSocketConnection("ws://localhost:8000", "ns", "db", protocol=protocol)
    ws = MagicMock()
    ws.send_bytes = AsyncMock()
    ws.send_str = AsyncMock()
    conn._ws = ws
    conn._connected = True
    return conn, ws


class TestWebSocketEncodingErrors:
    """An argument that cannot be encoded is reported as itself, before anything is sent."""

    async def test_out_of_range_decimal_raises_value_error(self) -> None:
        conn, ws = _connected("cbor")
        with pytest.raises(ValueError, match="out of range") as info:
            await conn.query("RETURN $p", {"p": Decimal("1E+29")})
        assert not isinstance(info.value, SurrealConnectionError)
        ws.send_bytes.assert_not_called()
        assert conn._pending == {}

    async def test_unsupported_type_raises_type_error_over_cbor(self) -> None:
        conn, ws = _connected("cbor")
        with pytest.raises(TypeError):
            await conn.query("RETURN $p", {"p": object()})
        ws.send_bytes.assert_not_called()
        assert conn._pending == {}

    async def test_unsupported_type_raises_type_error_over_json(self) -> None:
        conn, ws = _connected("json")
        with pytest.raises(TypeError):
            await conn.query("RETURN $p", {"p": object()})
        ws.send_str.assert_not_called()
        assert conn._pending == {}

    async def test_send_failure_is_still_a_connection_error(self) -> None:
        conn, ws = _connected("cbor")
        ws.send_bytes.side_effect = RuntimeError("socket closed")
        with pytest.raises(SurrealConnectionError, match="Request failed"):
            await conn.query("RETURN 1")
        assert conn._pending == {}
