"""Issue #441: the DIFF form of LIVE SELECT, and WebSocket encoding errors.

1. Every live-query builder appended ``DIFF`` after the table or the ``WHERE``
   clause (``LIVE SELECT * FROM t WHERE x = 1 DIFF``), which SurrealDB 3.x
   refuses as a parse error, so no DIFF subscription could start. The DIFF
   form is ``LIVE SELECT DIFF FROM t [WHERE ...]``; one helper renders it for
   all five builders.
2. ``WebSocketConnection._send_rpc()`` encoded the request inside the ``try``
   that turns every error into ``ConnectionError``, after registering the
   pending future. A value the encoder refuses reached the caller as
   ``ConnectionError`` although nothing was sent. It is encoded first now, so
   the original ``TypeError``/``ValueError`` reaches the caller and nothing is
   left in ``_pending``.
"""

from typing import Any

import pytest

from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.exceptions import ConnectionError as SdkConnectionError
from surreal_sdk.exceptions import TimeoutError as SdkTimeoutError
from surreal_sdk.protocol.rpc import RPCRequest, _live_select_sql
from surreal_sdk.streaming.live_query import LiveQuery
from surreal_sdk.streaming.live_select import LiveSelectStream, LiveSubscriptionParams
from surreal_sdk.types import QueryResponse


class _RecordingConnection:
    """Stands in for a ``WebSocketConnection``: records every ``query(sql, vars)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._live_callbacks: dict[str, Any] = {}
        self._live_subscriptions: dict[str, Any] = {}

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        self.calls.append((sql, vars))
        return QueryResponse.from_rpc_result([{"status": "OK", "result": "live-1", "time": "1ms"}])

    def _register_live_subscription(self, live_id: str, params: Any) -> None:
        self._live_subscriptions[live_id] = params

    def _unregister_live_subscription(self, live_id: str) -> None:
        self._live_subscriptions.pop(live_id, None)

    async def kill(self, live_id: str) -> None:
        pass


class TestLiveSelectSql:
    """The one renderer of a ``LIVE SELECT`` statement."""

    def test_plain(self) -> None:
        assert _live_select_sql("t") == "LIVE SELECT * FROM t"

    def test_where(self) -> None:
        assert _live_select_sql("t", where="x = $_f0") == "LIVE SELECT * FROM t WHERE x = $_f0"

    def test_diff(self) -> None:
        assert _live_select_sql("t", diff=True) == "LIVE SELECT DIFF FROM t"

    def test_diff_where(self) -> None:
        assert _live_select_sql("t", where="x = $_f0", diff=True) == "LIVE SELECT DIFF FROM t WHERE x = $_f0"

    def test_empty_where_is_no_where(self) -> None:
        assert _live_select_sql("t", where="", diff=True) == "LIVE SELECT DIFF FROM t"


class TestEveryBuilderEmitsTheDiffForm:
    """The five builders all send ``LIVE SELECT DIFF FROM ...`` in DIFF mode."""

    def test_rpc_request_live(self) -> None:
        request = RPCRequest.live("orders", diff=True)
        assert request.params[0] == "LIVE SELECT DIFF FROM orders"

    async def test_live_select_stream_start(self) -> None:
        conn = _RecordingConnection()
        stream = LiveSelectStream(conn, "orders", where="total > $_f0", params={"_f0": 5}, diff=True)  # type: ignore[arg-type]

        await stream.start()

        assert conn.calls == [("LIVE SELECT DIFF FROM orders WHERE total > $_f0", {"_f0": 5})]

    async def test_resubscribe_sends_the_same_text_as_start(self) -> None:
        start_conn = _RecordingConnection()
        stream = LiveSelectStream(start_conn, "orders", where="total > $_f0", params={"_f0": 5}, diff=True)  # type: ignore[arg-type]
        await stream.start()

        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        recorder = _RecordingConnection()
        conn.query = recorder.query  # type: ignore[method-assign]
        sub = LiveSubscriptionParams(
            table="orders", where="total > $_f0", params={"_f0": 5}, diff=True, callback=None, on_reconnect=None
        )

        await conn._resubscribe_one(sub)

        assert recorder.calls == start_conn.calls
        assert recorder.calls[0][0] == "LIVE SELECT DIFF FROM orders WHERE total > $_f0"

    async def test_websocket_live(self) -> None:
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        recorder = _RecordingConnection()
        conn.query = recorder.query  # type: ignore[method-assign]

        async def callback(_: Any) -> None:
            pass

        await conn.live("orders", callback, diff=True)

        assert recorder.calls == [("LIVE SELECT DIFF FROM orders", None)]

    async def test_live_query_subscribe(self) -> None:
        conn = _RecordingConnection()
        live = LiveQuery(conn, "orders", where="status = 'open'", diff=True)  # type: ignore[arg-type]

        async def callback(_: Any) -> None:
            pass

        await live.subscribe(callback)

        assert conn.calls == [("LIVE SELECT DIFF FROM orders WHERE status = 'open'", None)]


class TestDiffNotification:
    """A DIFF notification carries patches, and names its record beside them."""

    def test_record_id_comes_from_the_notification(self) -> None:
        from surreal_sdk.protocol.cbor import RecordId
        from surreal_sdk.streaming.live_select import LiveChange

        change = LiveChange.from_dict(
            {
                "action": "UPDATE",
                "id": "live-1",
                "record": RecordId(table="orders", id="a"),
                "result": [{"op": "replace", "path": "/total", "value": 9}],
            }
        )

        assert change.record_id == "orders:a"
        assert change.changed_fields == ["total"]
        assert change.result == {}

    def test_full_record_id_still_wins(self) -> None:
        from surreal_sdk.streaming.live_select import LiveChange

        change = LiveChange.from_dict({"action": "CREATE", "record": "other:x", "result": {"id": "orders:a"}})

        assert change.record_id == "orders:a"


class _FakeSocket:
    """A connected socket that records what is sent."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


def _connected(protocol: str) -> tuple[WebSocketConnection, _FakeSocket]:
    conn = WebSocketConnection("ws://localhost:8000", "ns", "db", protocol=protocol)  # type: ignore[arg-type]
    sock = _FakeSocket()
    conn._ws = sock  # type: ignore[assignment]
    conn._connected = True
    return conn, sock


class TestWebSocketEncodingErrors:
    """A value the encoder refuses is the caller's error, not a lost connection."""

    @pytest.mark.parametrize("protocol", ["cbor", "json"])
    async def test_unsupported_type_raises_type_error(self, protocol: str) -> None:
        conn, sock = _connected(protocol)

        with pytest.raises(TypeError) as excinfo:
            await conn.query("RETURN $x", {"x": object()})

        assert not isinstance(excinfo.value, SdkConnectionError)
        assert sock.sent == []
        assert conn._pending == {}

    async def test_lone_surrogate_raises_value_error_over_cbor(self) -> None:
        conn, sock = _connected("cbor")

        with pytest.raises(ValueError):
            await conn.query("RETURN $x", {"x": "\ud800"})

        assert sock.sent == []
        assert conn._pending == {}

    async def test_connection_still_usable_after_an_encoding_error(self) -> None:
        conn, sock = _connected("json")
        with pytest.raises(TypeError):
            await conn.query("RETURN $x", {"x": object()})

        # The next request is sent normally (its reply never comes: it times out).
        conn.timeout = 0.05
        with pytest.raises(SdkTimeoutError):
            await conn.query("RETURN 1")
        assert len(sock.sent) == 1
        assert conn._pending == {}
