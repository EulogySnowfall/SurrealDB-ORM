"""Tests for the RPC protocol module."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from src.surreal_sdk.protocol.rpc import RPCRequest, RPCResponse, RPCError, RPCMethod
from src.surreal_sdk.protocol.cbor import (
    CBOR_AVAILABLE,
    RecordId,
    Table,
    Duration,
    encode as cbor_encode,
    decode as cbor_decode,
    is_available as cbor_is_available,
)


class TestRPCRequest:
    """Tests for RPCRequest class."""

    def test_basic_request(self) -> None:
        """Test creating a basic RPC request."""
        request = RPCRequest(method="query", params=["SELECT * FROM users"])
        assert request.method == "query"
        assert request.params == ["SELECT * FROM users"]
        assert request.id == 1

    def test_to_dict(self) -> None:
        """Test converting request to dictionary."""
        request = RPCRequest(method="select", params=["users"], id=5)
        data = request.to_dict()

        assert data["id"] == 5
        assert data["method"] == "select"
        assert data["params"] == ["users"]

    def test_to_json(self) -> None:
        """Test JSON serialization."""
        request = RPCRequest(method="ping", params=[], id=1)
        json_str = request.to_json()

        assert '"method": "ping"' in json_str
        assert '"id": 1' in json_str

    def test_query_factory(self) -> None:
        """Test query factory method."""
        request = RPCRequest.query("SELECT * FROM users WHERE age > 21", {"limit": 10})

        assert request.method == "query"
        assert request.params[0] == "SELECT * FROM users WHERE age > 21"
        assert request.params[1] == {"limit": 10}

    def test_select_factory(self) -> None:
        """Test select factory method."""
        request = RPCRequest.select("users:123")

        assert request.method == "select"
        assert request.params == ["users:123"]

    def test_create_factory(self) -> None:
        """Test create factory method."""
        request = RPCRequest.create("users", {"name": "Alice", "age": 30})

        assert request.method == "create"
        assert request.params[0] == "users"
        assert request.params[1] == {"name": "Alice", "age": 30}

    def test_signin_factory(self) -> None:
        """Test signin factory method."""
        request = RPCRequest.signin(user="root", password="root", namespace="test")

        assert request.method == "signin"
        assert request.params["user"] == "root"
        assert request.params["pass"] == "root"
        assert request.params["ns"] == "test"

    def test_use_factory(self) -> None:
        """Test use factory method."""
        request = RPCRequest.use("my_namespace", "my_database")

        assert request.method == "use"
        assert request.params == ["my_namespace", "my_database"]

    def test_live_factory(self) -> None:
        """Test live query factory method."""
        request = RPCRequest.live("orders", diff=True)

        assert request.method == "query"
        assert "LIVE SELECT * FROM orders DIFF" in request.params[0]

    def test_kill_factory(self) -> None:
        """Test kill factory method."""
        request = RPCRequest.kill("some-uuid-here")

        assert request.method == "kill"
        assert request.params == ["some-uuid-here"]


class TestRPCResponse:
    """Tests for RPCResponse class."""

    def test_success_response(self) -> None:
        """Test parsing successful response."""
        data = {"id": 1, "result": [{"id": "users:1", "name": "Alice"}]}
        response = RPCResponse.from_dict(data)

        assert response.id == 1
        assert response.is_success
        assert not response.is_error
        assert response.result == [{"id": "users:1", "name": "Alice"}]

    def test_error_response(self) -> None:
        """Test parsing error response."""
        data = {"id": 1, "error": {"code": -32000, "message": "Table 'users' does not exist"}}
        response = RPCResponse.from_dict(data)

        assert response.id == 1
        assert response.is_error
        assert not response.is_success
        assert response.error is not None
        assert response.error.code == -32000
        assert "does not exist" in response.error.message

    def test_from_json(self) -> None:
        """Test parsing from JSON string."""
        json_str = '{"id": 5, "result": "ok"}'
        response = RPCResponse.from_json(json_str)

        assert response.id == 5
        assert response.result == "ok"


class TestRPCError:
    """Tests for RPCError class."""

    def test_from_dict(self) -> None:
        """Test creating error from dict."""
        data = {"code": -32600, "message": "Invalid request"}
        error = RPCError.from_dict(data)

        assert error.code == -32600
        assert error.message == "Invalid request"

    def test_from_dict_defaults(self) -> None:
        """Test default values."""
        error = RPCError.from_dict({})

        assert error.code == -1
        assert error.message == "Unknown error"


class TestRPCMethod:
    """Tests for RPCMethod constants."""

    def test_method_constants(self) -> None:
        """Test method name constants."""
        assert RPCMethod.SIGNIN == "signin"
        assert RPCMethod.QUERY == "query"
        assert RPCMethod.SELECT == "select"
        assert RPCMethod.CREATE == "create"
        assert RPCMethod.UPDATE == "update"
        assert RPCMethod.DELETE == "delete"
        assert RPCMethod.LIVE == "live"
        assert RPCMethod.KILL == "kill"


# =============================================================================
# CBOR Protocol Tests
# =============================================================================


class TestCBORTypes:
    """Tests for CBOR type classes."""

    def test_record_id_str(self) -> None:
        """Test RecordId string representation."""
        record_id = RecordId(table="users", id="abc123")
        assert str(record_id) == "users:abc123"

    def test_record_id_parse(self) -> None:
        """Test RecordId parsing from string."""
        record_id = RecordId.parse("users:abc123")
        assert record_id.table == "users"
        assert record_id.id == "abc123"

    def test_record_id_parse_with_colons_in_id(self) -> None:
        """Test RecordId parsing with colons in the ID (like UUIDs)."""
        record_id = RecordId.parse("users:550e8400-e29b-41d4-a716-446655440000")
        assert record_id.table == "users"
        assert record_id.id == "550e8400-e29b-41d4-a716-446655440000"

    def test_record_id_parse_invalid(self) -> None:
        """Test RecordId parsing with invalid format."""
        with pytest.raises(ValueError, match="Invalid record ID format"):
            RecordId.parse("invalid")

    def test_table_str(self) -> None:
        """Test Table string representation."""
        table = Table(name="users")
        assert str(table) == "users"

    def test_duration_str(self) -> None:
        """Test Duration string representation."""
        duration = Duration(value="1h30m")
        assert str(duration) == "1h30m"


class TestCBORAvailability:
    """Tests for CBOR availability checking."""

    def test_cbor_is_available_function(self) -> None:
        """Test cbor_is_available() function - always True since cbor2 is required."""
        result = cbor_is_available()
        assert result is True
        assert CBOR_AVAILABLE is True


class TestCBOREncodeDecode:
    """Tests for CBOR encoding/decoding (requires cbor2)."""

    def test_encode_decode_basic_types(self) -> None:
        """Test encoding and decoding basic Python types."""
        data = {
            "string": "hello",
            "number": 42,
            "float": 3.14,
            "bool": True,
            "list": [1, 2, 3],
        }
        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        assert decoded == data

    def test_encode_decode_datetime(self) -> None:
        """Test encoding and decoding datetime."""
        now = datetime.now(timezone.utc)
        data = {"timestamp": now}

        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        assert decoded["timestamp"] == now

    def test_encode_decode_uuid(self) -> None:
        """Test encoding and decoding UUID."""
        uid = UUID("550e8400-e29b-41d4-a716-446655440000")
        data = {"uuid": uid}

        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        assert decoded["uuid"] == uid

    def test_encode_decode_decimal(self) -> None:
        """Test encoding and decoding Decimal."""
        dec = Decimal("123.456789")
        data = {"price": dec}

        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        assert decoded["price"] == dec

    def test_encode_decode_record_id(self) -> None:
        """Test encoding and decoding RecordId."""
        record_id = RecordId(table="users", id="abc123")
        data = {"ref": record_id}

        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        assert isinstance(decoded["ref"], RecordId)
        assert decoded["ref"].table == "users"
        assert decoded["ref"].id == "abc123"

    def test_encode_decode_table(self) -> None:
        """Test encoding and decoding Table."""
        table = Table(name="users")
        data = {"table": table}

        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        assert isinstance(decoded["table"], Table)
        assert decoded["table"].name == "users"

    def test_encode_decode_duration(self) -> None:
        """Test encoding and decoding Duration."""
        duration = Duration(value="1h30m")
        data = {"timeout": duration}

        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        assert isinstance(decoded["timeout"], Duration)
        assert decoded["timeout"].value == "1h30m"

    def test_data_prefix_string_not_interpreted_as_record_link(self) -> None:
        """
        Test that strings with 'data:' prefix are NOT interpreted as record links.

        This is the key fix for Issue #3: In JSON protocol, SurrealDB interprets
        'data:xxx' as a record link. With CBOR, it's properly encoded as a string.
        """
        # This would be incorrectly interpreted as a record link in JSON
        base64_image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        data = {
            "content": base64_image,
            "type": "image",
        }

        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        # The string should be preserved exactly as-is
        assert decoded["content"] == base64_image
        assert isinstance(decoded["content"], str)
        # It should NOT be converted to a RecordId or any other type
        assert not isinstance(decoded["content"], RecordId)

    def test_multiple_data_prefix_strings(self) -> None:
        """Test multiple data: prefix strings in same document."""
        data = {
            "avatar": "data:image/jpeg;base64,/9j/4AAQSkZJRg==",
            "thumbnail": "data:image/png;base64,iVBORw0KGgo=",
            "document": "data:application/pdf;base64,JVBERi0xLjQ=",
        }

        encoded = cbor_encode(data)
        decoded = cbor_decode(encoded)

        assert decoded["avatar"] == data["avatar"]
        assert decoded["thumbnail"] == data["thumbnail"]
        assert decoded["document"] == data["document"]

    def test_rpc_request_to_cbor(self) -> None:
        """Test RPCRequest.to_cbor() method."""
        request = RPCRequest(
            method="create",
            params=["files", {"content": "data:image/png;base64,abc123"}],
            id=42,
        )

        cbor_data = request.to_cbor()
        decoded = cbor_decode(cbor_data)

        assert decoded["id"] == 42
        assert decoded["method"] == "create"
        assert decoded["params"][0] == "files"
        assert decoded["params"][1]["content"] == "data:image/png;base64,abc123"

    def test_rpc_response_from_cbor(self) -> None:
        """Test RPCResponse.from_cbor() method."""
        response_data = {
            "id": 42,
            "result": [{"id": "files:1", "content": "data:image/png;base64,abc123"}],
        }

        cbor_data = cbor_encode(response_data)
        response = RPCResponse.from_cbor(cbor_data)

        assert response.id == 42
        assert response.is_success
        assert response.result[0]["content"] == "data:image/png;base64,abc123"
