"""Unit tests for surreal_sdk.protocol — CBOR and RPC protocol."""

import json
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID

import pytest

from surreal_sdk.protocol.cbor import (
    Duration,
    RecordId,
    Table,
    decode,
    encode,
    is_available,
)
from surreal_sdk.protocol.rpc import (
    RPCError,
    RPCMethod,
    RPCRequest,
    RPCResponse,
    SurrealJSONEncoder,
)

# ── CBOR Types ──────────────────────────────────────────────────────


class TestRecordId:
    def test_str(self) -> None:
        rid = RecordId(table="users", id="abc123")
        assert str(rid) == "users:abc123"

    def test_str_numeric_id(self) -> None:
        rid = RecordId(table="users", id=42)
        assert str(rid) == "users:42"

    def test_parse_valid(self) -> None:
        rid = RecordId.parse("users:abc123")
        assert rid.table == "users"
        assert rid.id == "abc123"

    def test_parse_with_complex_id(self) -> None:
        rid = RecordId.parse("users:some:complex:id")
        assert rid.table == "users"
        assert rid.id == "some:complex:id"

    def test_parse_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid record ID"):
            RecordId.parse("no_colon_here")


class TestTable:
    def test_str(self) -> None:
        tbl = Table(name="users")
        assert str(tbl) == "users"


class TestDuration:
    def test_str(self) -> None:
        dur = Duration(value="30d")
        assert str(dur) == "30d"

    def test_str_complex(self) -> None:
        dur = Duration(value="2h30m")
        assert str(dur) == "2h30m"


# ── CBOR Encode/Decode ────────────────────────────────────────────


class TestCBOREncodeDecode:
    def test_record_id_roundtrip(self) -> None:
        original = RecordId(table="users", id="abc")
        encoded = encode(original)
        decoded = decode(encoded)
        assert isinstance(decoded, RecordId)
        assert decoded.table == "users"
        assert decoded.id == "abc"

    def test_table_roundtrip(self) -> None:
        original = Table(name="users")
        encoded = encode(original)
        decoded = decode(encoded)
        assert isinstance(decoded, Table)
        assert decoded.name == "users"

    def test_duration_roundtrip(self) -> None:
        original = Duration(value="2h")
        encoded = encode(original)
        decoded = decode(encoded)
        assert isinstance(decoded, Duration)
        assert decoded.value == "2h"

    def test_datetime_roundtrip(self) -> None:
        original = datetime(2026, 2, 11, 12, 0, 0, tzinfo=UTC)
        encoded = encode(original)
        decoded = decode(encoded)
        assert isinstance(decoded, datetime)
        assert decoded.year == 2026
        assert decoded.month == 2

    def test_datetime_with_tz(self) -> None:
        original = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        encoded = encode(original)
        decoded = decode(encoded)
        assert decoded.tzinfo is not None
        assert decoded.year == 2026

    def test_uuid_roundtrip(self) -> None:
        original = UUID("550e8400-e29b-41d4-a716-446655440000")
        encoded = encode(original)
        decoded = decode(encoded)
        assert isinstance(decoded, UUID)
        assert decoded == original

    def test_decimal_roundtrip(self) -> None:
        original = Decimal("123.456")
        encoded = encode(original)
        decoded = decode(encoded)
        assert isinstance(decoded, Decimal)
        assert decoded == original

    def test_none_roundtrip(self) -> None:
        encoded = encode(None)
        decoded = decode(encoded)
        assert decoded is None

    def test_unsupported_type_raises(self) -> None:
        class Custom:
            pass

        with pytest.raises(TypeError, match="Cannot CBOR encode"):
            encode(Custom())

    def test_simple_types_passthrough(self) -> None:
        # Strings, ints, lists, dicts go through standard CBOR
        assert decode(encode("hello")) == "hello"
        assert decode(encode(42)) == 42
        assert decode(encode([1, 2, 3])) == [1, 2, 3]
        assert decode(encode({"key": "value"})) == {"key": "value"}

    def test_is_available(self) -> None:
        assert is_available() is True

    def test_nested_structure(self) -> None:
        data = {
            "id": RecordId(table="users", id="abc"),
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "score": Decimal("99.5"),
        }
        encoded = encode(data)
        decoded = decode(encoded)
        assert isinstance(decoded["id"], RecordId)
        assert isinstance(decoded["created_at"], datetime)
        assert isinstance(decoded["score"], Decimal)


# ── JSON Encoder ──────────────────────────────────────────────────


class TestSurrealJSONEncoder:
    def test_datetime(self) -> None:
        dt = datetime(2026, 2, 11, 12, 0, 0)
        result = json.dumps({"dt": dt}, cls=SurrealJSONEncoder)
        assert "2026-02-11" in result

    def test_date(self) -> None:
        d = date(2026, 2, 11)
        result = json.dumps({"d": d}, cls=SurrealJSONEncoder)
        assert "2026-02-11" in result

    def test_time(self) -> None:
        t = time(12, 30, 45)
        result = json.dumps({"t": t}, cls=SurrealJSONEncoder)
        assert "12:30:45" in result

    def test_decimal(self) -> None:
        dec = Decimal("123.45")
        result = json.dumps({"val": dec}, cls=SurrealJSONEncoder)
        assert "123.45" in result

    def test_uuid(self) -> None:
        uid = UUID("550e8400-e29b-41d4-a716-446655440000")
        result = json.dumps({"id": uid}, cls=SurrealJSONEncoder)
        assert "550e8400" in result

    def test_unsupported_type(self) -> None:
        with pytest.raises(TypeError):
            json.dumps({"val": object()}, cls=SurrealJSONEncoder)


# ── RPC Request ──────────────────────────────────────────────────


class TestRPCRequest:
    def test_to_dict(self) -> None:
        req = RPCRequest(method="query", params=["SELECT *", {}], id=1)
        d = req.to_dict()
        assert d["method"] == "query"
        assert d["id"] == 1
        assert isinstance(d["params"], list)

    def test_to_dict_dict_params(self) -> None:
        req = RPCRequest(method="signin", params={"user": "root"}, id=1)
        d = req.to_dict()
        # Dict params get wrapped in a list
        assert isinstance(d["params"], list)
        assert d["params"][0] == {"user": "root"}

    def test_to_json(self) -> None:
        req = RPCRequest.query("SELECT * FROM users")
        json_str = req.to_json()
        assert "query" in json_str
        assert "SELECT" in json_str

    def test_to_cbor(self) -> None:
        req = RPCRequest.query("SELECT * FROM users")
        cbor_bytes = req.to_cbor()
        assert isinstance(cbor_bytes, bytes)

    def test_query_factory(self) -> None:
        req = RPCRequest.query("SELECT * FROM users", vars={"x": 1}, request_id=5)
        assert req.method == "query"
        assert req.params == ["SELECT * FROM users", {"x": 1}]
        assert req.id == 5

    def test_query_factory_no_vars(self) -> None:
        req = RPCRequest.query("SELECT 1")
        assert req.params == ["SELECT 1", {}]

    def test_select_factory(self) -> None:
        req = RPCRequest.select("users")
        assert req.method == "select"
        assert req.params == ["users"]

    def test_create_factory(self) -> None:
        req = RPCRequest.create("users", {"name": "Alice"})
        assert req.method == "create"
        assert req.params == ["users", {"name": "Alice"}]

    def test_update_factory(self) -> None:
        req = RPCRequest.update("users:1", {"name": "Bob"})
        assert req.method == "update"

    def test_merge_factory(self) -> None:
        req = RPCRequest.merge("users:1", {"age": 30})
        assert req.method == "merge"

    def test_delete_factory(self) -> None:
        req = RPCRequest.delete("users:1")
        assert req.method == "delete"
        assert req.params == ["users:1"]

    def test_signin_factory_full(self) -> None:
        req = RPCRequest.signin(user="root", password="root", namespace="test", database="test", access="myaccess")
        assert req.method == "signin"
        params = req.params
        assert params["user"] == "root"
        assert params["pass"] == "root"
        assert params["ns"] == "test"
        assert params["db"] == "test"
        assert params["ac"] == "myaccess"

    def test_signin_factory_minimal(self) -> None:
        req = RPCRequest.signin(user="root", password="root")
        params = req.params
        assert "ns" not in params
        assert "db" not in params

    def test_use_factory(self) -> None:
        req = RPCRequest.use("myns", "mydb")
        assert req.method == "use"
        assert req.params == ["myns", "mydb"]

    def test_live_factory(self) -> None:
        req = RPCRequest.live("users")
        assert req.method == "query"
        assert "LIVE SELECT * FROM users" in req.params[0]
        assert "DIFF" not in req.params[0]

    def test_live_factory_diff(self) -> None:
        req = RPCRequest.live("users", diff=True)
        assert "DIFF" in req.params[0]

    def test_kill_factory(self) -> None:
        req = RPCRequest.kill("uuid-123")
        assert req.method == "kill"
        assert req.params == ["uuid-123"]


# ── RPC Error ────────────────────────────────────────────────────


class TestRPCError:
    def test_from_dict(self) -> None:
        err = RPCError.from_dict({"code": 500, "message": "Internal error"})
        assert err.code == 500
        assert err.message == "Internal error"

    def test_from_dict_defaults(self) -> None:
        err = RPCError.from_dict({})
        assert err.code == -1
        assert err.message == "Unknown error"


# ── RPC Response ────────────────────────────────────────────────


class TestRPCResponse:
    def test_success(self) -> None:
        resp = RPCResponse(id=1, result=[{"id": "1"}])
        assert resp.is_success is True
        assert resp.is_error is False

    def test_error(self) -> None:
        err = RPCError(code=500, message="error")
        resp = RPCResponse(id=1, error=err)
        assert resp.is_error is True
        assert resp.is_success is False

    def test_from_dict_success(self) -> None:
        resp = RPCResponse.from_dict({"id": 1, "result": [{"id": "1"}]})
        assert resp.is_success is True
        assert resp.result == [{"id": "1"}]

    def test_from_dict_error(self) -> None:
        resp = RPCResponse.from_dict(
            {
                "id": 1,
                "error": {"code": 400, "message": "parse error"},
            }
        )
        assert resp.is_error is True
        assert resp.error is not None
        assert resp.error.code == 400

    def test_from_dict_defaults(self) -> None:
        resp = RPCResponse.from_dict({})
        assert resp.id == 0
        assert resp.result is None

    def test_from_json(self) -> None:
        json_str = '{"id": 1, "result": [{"id": "1"}]}'
        resp = RPCResponse.from_json(json_str)
        assert resp.id == 1
        assert resp.is_success is True

    def test_from_cbor(self) -> None:
        import cbor2

        data = {"id": 1, "result": []}
        cbor_bytes = cbor2.dumps(data)
        resp = RPCResponse.from_cbor(cbor_bytes)
        assert resp.id == 1
        assert resp.is_success is True


# ── RPC Method Constants ────────────────────────────────────────


class TestRPCMethod:
    def test_auth_methods(self) -> None:
        assert RPCMethod.SIGNIN == "signin"
        assert RPCMethod.SIGNUP == "signup"
        assert RPCMethod.AUTHENTICATE == "authenticate"
        assert RPCMethod.INVALIDATE == "invalidate"
        assert RPCMethod.INFO == "info"

    def test_connection_methods(self) -> None:
        assert RPCMethod.USE == "use"
        assert RPCMethod.PING == "ping"
        assert RPCMethod.VERSION == "version"
        assert RPCMethod.RESET == "reset"

    def test_crud_methods(self) -> None:
        assert RPCMethod.SELECT == "select"
        assert RPCMethod.CREATE == "create"
        assert RPCMethod.INSERT == "insert"
        assert RPCMethod.UPDATE == "update"
        assert RPCMethod.UPSERT == "upsert"
        assert RPCMethod.MERGE == "merge"
        assert RPCMethod.PATCH == "patch"
        assert RPCMethod.DELETE == "delete"
        assert RPCMethod.RELATE == "relate"

    def test_query_methods(self) -> None:
        assert RPCMethod.QUERY == "query"
        assert RPCMethod.GRAPHQL == "graphql"
        assert RPCMethod.RUN == "run"

    def test_live_methods(self) -> None:
        assert RPCMethod.LIVE == "live"
        assert RPCMethod.KILL == "kill"

    def test_variable_methods(self) -> None:
        assert RPCMethod.LET == "let"
        assert RPCMethod.UNSET == "unset"
