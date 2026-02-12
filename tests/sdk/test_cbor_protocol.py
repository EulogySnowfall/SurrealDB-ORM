"""Tests for CBOR protocol support in the SDK."""

from datetime import UTC, datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from src.surreal_sdk.protocol import cbor as cbor_module
from src.surreal_sdk.protocol.cbor import (
    TAG_DATETIME,
    TAG_NONE,
    TAG_RECORDID,
    TAG_STRING_DECIMAL,
    TAG_STRING_DURATION,
    TAG_STRING_UUID,
    TAG_TABLE,
    Duration,
    RecordId,
    Table,
    decode,
    encode,
    is_available,
)
from src.surreal_sdk.protocol.rpc import RPCRequest, RPCResponse


class TestCBORAvailability:
    """Test CBOR availability."""

    def test_cbor_is_available(self) -> None:
        """CBOR should always be available (required dependency)."""
        assert is_available() is True
        assert cbor_module.CBOR_AVAILABLE is True


class TestCBORDataclasses:
    """Test CBOR dataclass types."""

    def test_record_id_str(self) -> None:
        """RecordId __str__ should return table:id format."""
        record_id = RecordId(table="users", id="abc123")
        assert str(record_id) == "users:abc123"

    def test_record_id_parse(self) -> None:
        """RecordId.parse should parse table:id format."""
        record_id = RecordId.parse("users:abc123")
        assert record_id.table == "users"
        assert record_id.id == "abc123"

    def test_record_id_parse_invalid(self) -> None:
        """RecordId.parse should raise on invalid format."""
        with pytest.raises(ValueError, match="Invalid record ID format"):
            RecordId.parse("invalid_no_colon")

    def test_table_str(self) -> None:
        """Table __str__ should return table name."""
        table = Table(name="users")
        assert str(table) == "users"

    def test_duration_str(self) -> None:
        """Duration __str__ should return duration value."""
        duration = Duration(value="1h30m")
        assert str(duration) == "1h30m"


class TestCBOREncodeDecode:
    """Test CBOR encoding and decoding."""

    def test_encode_decode_string(self) -> None:
        """Encode and decode simple string."""
        data = "hello world"
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_encode_decode_int(self) -> None:
        """Encode and decode integer."""
        data = 42
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_encode_decode_float(self) -> None:
        """Encode and decode float."""
        data = 3.14159
        encoded = encode(data)
        decoded = decode(encoded)
        assert abs(decoded - data) < 0.0001

    def test_encode_decode_list(self) -> None:
        """Encode and decode list."""
        data = [1, 2, 3, "hello"]
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_encode_decode_dict(self) -> None:
        """Encode and decode dictionary."""
        data = {"name": "Alice", "age": 30, "active": True}
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_encode_decode_nested(self) -> None:
        """Encode and decode nested structures."""
        data = {
            "user": {"name": "Bob", "id": 123},
            "items": [1, 2, {"nested": True}],
        }
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_encode_decode_none(self) -> None:
        """Encode and decode None with custom tag."""
        data = None
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded is None

    def test_encode_decode_datetime(self) -> None:
        """Encode and decode datetime with custom tag."""
        data = datetime(2026, 2, 3, 12, 30, 45, tzinfo=UTC)
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_encode_decode_datetime_with_tz(self) -> None:
        """Encode datetime with timezone."""
        from datetime import timedelta

        # Use a non-UTC timezone
        tz = timezone(timedelta(hours=-5))
        data = datetime(2026, 2, 3, 12, 30, 45, tzinfo=tz)
        encoded = encode(data)
        decoded = decode(encoded)
        # Should preserve timezone info
        assert decoded.tzinfo is not None

    def test_encode_decode_uuid(self) -> None:
        """Encode and decode UUID with custom tag."""
        data = UUID("12345678-1234-5678-1234-567812345678")
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_encode_decode_decimal(self) -> None:
        """Encode and decode Decimal with custom tag."""
        data = Decimal("123.456789")
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded == data

    def test_encode_decode_record_id(self) -> None:
        """Encode and decode RecordId with custom tag."""
        data = RecordId(table="users", id="abc123")
        encoded = encode(data)
        decoded = decode(encoded)
        assert isinstance(decoded, RecordId)
        assert decoded.table == "users"
        assert decoded.id == "abc123"

    def test_encode_decode_table(self) -> None:
        """Encode and decode Table with custom tag."""
        data = Table(name="users")
        encoded = encode(data)
        decoded = decode(encoded)
        assert isinstance(decoded, Table)
        assert decoded.name == "users"

    def test_encode_decode_duration(self) -> None:
        """Encode and decode Duration with custom tag."""
        data = Duration(value="1h30m")
        encoded = encode(data)
        decoded = decode(encoded)
        assert isinstance(decoded, Duration)
        assert decoded.value == "1h30m"

    def test_encode_unsupported_type_raises(self) -> None:
        """Encoding unsupported types should raise TypeError."""

        class CustomClass:
            pass

        with pytest.raises(TypeError, match="Cannot CBOR encode"):
            encode(CustomClass())


class TestCBORTags:
    """Test CBOR tag constants."""

    def test_tag_constants(self) -> None:
        """CBOR tag constants should have correct values."""
        assert TAG_NONE == 6
        assert TAG_TABLE == 7
        assert TAG_RECORDID == 8
        assert TAG_STRING_UUID == 9
        assert TAG_STRING_DECIMAL == 10
        assert TAG_DATETIME == 12
        assert TAG_STRING_DURATION == 14


class TestRPCRequestCBOR:
    """Test RPC request CBOR serialization."""

    def test_rpc_request_to_cbor(self) -> None:
        """RPCRequest should serialize to CBOR."""
        request = RPCRequest(id=1, method="query", params=["SELECT * FROM users"])
        cbor_data = request.to_cbor()
        assert isinstance(cbor_data, bytes)
        assert len(cbor_data) > 0

    def test_rpc_request_to_cbor_with_datetime(self) -> None:
        """RPCRequest should serialize datetime params to CBOR."""
        now = datetime.now(UTC)
        request = RPCRequest(
            id=1,
            method="create",
            params=["users", {"created_at": now}],
        )
        cbor_data = request.to_cbor()
        assert isinstance(cbor_data, bytes)

    def test_rpc_request_to_cbor_with_uuid(self) -> None:
        """RPCRequest should serialize UUID params to CBOR."""
        user_id = UUID("12345678-1234-5678-1234-567812345678")
        request = RPCRequest(
            id=1,
            method="create",
            params=["users", {"id": user_id}],
        )
        cbor_data = request.to_cbor()
        assert isinstance(cbor_data, bytes)


class TestRPCResponseCBOR:
    """Test RPC response CBOR deserialization."""

    def test_rpc_response_from_cbor_success(self) -> None:
        """RPCResponse should deserialize from CBOR success response."""
        # Simulate a CBOR-encoded response
        response_data = {"id": 1, "result": [{"id": "users:1", "name": "Alice"}]}
        cbor_bytes = encode(response_data)

        response = RPCResponse.from_cbor(cbor_bytes)
        assert response.id == 1
        assert response.result == [{"id": "users:1", "name": "Alice"}]
        assert response.error is None

    def test_rpc_response_from_cbor_error(self) -> None:
        """RPCResponse should deserialize from CBOR error response."""
        response_data = {
            "id": 1,
            "error": {"code": -32600, "message": "Invalid request"},
        }
        cbor_bytes = encode(response_data)

        response = RPCResponse.from_cbor(cbor_bytes)
        assert response.id == 1
        assert response.error is not None
        assert response.error.code == -32600
        assert response.error.message == "Invalid request"

    def test_rpc_response_from_cbor_with_record_id(self) -> None:
        """RPCResponse should deserialize RecordId from CBOR."""
        record_id = RecordId(table="users", id="abc123")
        response_data = {"id": 1, "result": record_id}
        cbor_bytes = encode(response_data)

        response = RPCResponse.from_cbor(cbor_bytes)
        assert response.result is not None
        assert isinstance(response.result, RecordId)
        assert response.result.table == "users"
        assert response.result.id == "abc123"


class TestDataUrlStringsNotRecordLinks:
    """Test that data: URL strings are not misinterpreted as record links."""

    def test_encode_data_url_string(self) -> None:
        """Data URL strings should encode as plain strings, not record links."""
        data_url = "data:image/png;base64,iVBORw0KGgo="

        encoded = encode(data_url)
        decoded = decode(encoded)

        # Should decode as string, not as a RecordId or special type
        assert isinstance(decoded, str)
        assert decoded == data_url

    def test_encode_dict_with_data_url(self) -> None:
        """Dict containing data URL should preserve it as string."""
        data = {
            "name": "Avatar",
            "image": "data:image/png;base64,iVBORw0KGgo=",
        }

        encoded = encode(data)
        decoded = decode(encoded)

        assert decoded["image"] == data["image"]
        assert isinstance(decoded["image"], str)

    def test_encode_other_colon_prefixed_strings(self) -> None:
        """Other colon-prefixed strings should also be preserved."""
        test_strings = [
            "mailto:user@example.com",
            "tel:+1234567890",
            "http:example.com",  # Note: no double slash
            "custom:value",
        ]

        for s in test_strings:
            encoded = encode(s)
            decoded = decode(encoded)
            assert decoded == s
            assert isinstance(decoded, str)
