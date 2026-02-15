"""
Unit tests for v0.14.2 fixes.

Tests for:
- Issue #1: validate_token() cache + validate_token_local()
- Issue #2: CBOR None → NONE encoding
- Issue #3: flexible_fields in SurrealConfigDict
- Issue #5: validate_assignment=True (datetime coercion)
"""

import base64
import json
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cbor2 import CBORTag

from src.surreal_orm.auth.mixins import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
)
from src.surreal_orm.types import TableType
from src.surreal_sdk.protocol.cbor import (
    TAG_NONE,
    _preprocess_for_cbor,
    decode,
    encode,
)
from src.surreal_sdk.protocol.rpc import RPCRequest, _strip_none_values
from src.surreal_sdk.types import AuthResponse


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear model registry before each test."""
    clear_model_registry()


# =============================================================================
# Issue #2: CBOR None → NONE encoding
# =============================================================================


class TestCBORNoneEncoding:
    """None values must be encoded as CBORTag(6, None) (NONE), not CBOR null (NULL)."""

    def test_preprocess_none_toplevel(self) -> None:
        """Top-level None becomes CBORTag(TAG_NONE)."""
        result = _preprocess_for_cbor(None)
        assert isinstance(result, CBORTag)
        assert result.tag == TAG_NONE

    def test_preprocess_none_in_dict(self) -> None:
        """None values in dicts become CBORTag(TAG_NONE)."""
        result = _preprocess_for_cbor({"a": 1, "b": None, "c": "hello"})
        assert result["a"] == 1
        assert isinstance(result["b"], CBORTag)
        assert result["b"].tag == TAG_NONE
        assert result["c"] == "hello"

    def test_preprocess_none_in_list(self) -> None:
        """None values in lists become CBORTag(TAG_NONE)."""
        result = _preprocess_for_cbor([1, None, "hello"])
        assert result[0] == 1
        assert isinstance(result[1], CBORTag)
        assert result[1].tag == TAG_NONE
        assert result[2] == "hello"

    def test_preprocess_nested_none(self) -> None:
        """None values in deeply nested structures are replaced."""
        data = {
            "user": {"name": "Alice", "age": None},
            "items": [{"value": None}, {"value": 42}],
        }
        result = _preprocess_for_cbor(data)
        assert isinstance(result["user"]["age"], CBORTag)
        assert isinstance(result["items"][0]["value"], CBORTag)
        assert result["items"][1]["value"] == 42

    def test_preprocess_preserves_non_none(self) -> None:
        """Non-None values are not modified."""
        data = {"a": 1, "b": "hello", "c": True, "d": [1, 2, 3]}
        result = _preprocess_for_cbor(data)
        assert result == data

    def test_encode_decode_none_roundtrip(self) -> None:
        """None should roundtrip through encode/decode correctly."""
        data = None
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded is None

    def test_encode_decode_dict_with_none_roundtrip(self) -> None:
        """Dict with None values should roundtrip through encode/decode."""
        data = {"name": "Alice", "age": None, "active": True}
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded["name"] == "Alice"
        assert decoded["age"] is None
        assert decoded["active"] is True

    def test_encode_decode_nested_none_roundtrip(self) -> None:
        """Nested None values should roundtrip correctly."""
        data = {
            "user": {"name": "Bob", "metadata": None},
            "scores": [10, None, 30],
        }
        encoded = encode(data)
        decoded = decode(encoded)
        assert decoded["user"]["name"] == "Bob"
        assert decoded["user"]["metadata"] is None
        assert decoded["scores"] == [10, None, 30]


class TestJSONNoneStripping:
    """JSON protocol should strip None values from dicts."""

    def test_strip_none_from_flat_dict(self) -> None:
        """None values should be removed from flat dicts."""
        data = {"a": 1, "b": None, "c": "hello"}
        result = _strip_none_values(data)
        assert result == {"a": 1, "c": "hello"}

    def test_strip_none_from_nested_dict(self) -> None:
        """None values should be removed from nested dicts."""
        data = {"user": {"name": "Alice", "age": None}, "active": True}
        result = _strip_none_values(data)
        assert result == {"user": {"name": "Alice"}, "active": True}

    def test_strip_none_preserves_list_nones(self) -> None:
        """None values in lists are kept (lists are positional)."""
        data = {"items": [1, None, 3]}
        result = _strip_none_values(data)
        assert result == {"items": [1, None, 3]}

    def test_strip_none_empty_after_strip(self) -> None:
        """Dict that becomes empty after stripping returns empty dict."""
        data = {"a": None, "b": None}
        result = _strip_none_values(data)
        assert result == {}

    def test_rpc_request_to_json_strips_none(self) -> None:
        """RPCRequest.to_json() should strip None from params."""
        request = RPCRequest(
            method="create",
            params=["users", {"name": "Alice", "age": None}],
        )
        json_str = request.to_json()
        parsed = json.loads(json_str)
        # The data dict inside params should not have "age"
        data_param = parsed["params"][1]
        assert "age" not in data_param
        assert data_param["name"] == "Alice"


# =============================================================================
# Issue #5: validate_assignment=True (datetime coercion)
# =============================================================================


class TestValidateAssignment:
    """BaseSurrealModel should auto-validate field assignments."""

    def test_datetime_from_iso_string_assignment(self) -> None:
        """Assigning an ISO string to a datetime field should auto-convert."""

        class Event(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="events")
            id: str | None = None
            name: str
            started_at: datetime | None = None

        event = Event(name="test")
        event.started_at = "2026-02-13T10:00:00Z"  # type: ignore[assignment]
        assert isinstance(event.started_at, datetime)
        assert event.started_at.year == 2026
        assert event.started_at.month == 2
        assert event.started_at.day == 13

    def test_datetime_object_assignment(self) -> None:
        """Assigning a datetime object should work normally."""

        class Event(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="events2")
            id: str | None = None
            name: str
            started_at: datetime | None = None

        now = datetime.now(UTC)
        event = Event(name="test")
        event.started_at = now
        assert event.started_at == now

    def test_int_field_coercion(self) -> None:
        """Assigning a string number to an int field should auto-convert."""

        class Item(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="items")
            id: str | None = None
            count: int = 0

        item = Item()
        item.count = "42"  # type: ignore[assignment]
        assert item.count == 42
        assert isinstance(item.count, int)


# =============================================================================
# Issue #1: Token cache + validate_token_local
# =============================================================================


class TestTokenCache:
    """Token validation cache tests."""

    @pytest.fixture(autouse=True)
    def clear_cache(self) -> None:
        """Clear token cache before each test."""
        AuthenticatedUserMixin._token_cache.clear()
        AuthenticatedUserMixin._token_cache_ttl = 300

    def test_configure_token_cache_ttl(self) -> None:
        """configure_token_cache should set the TTL."""
        AuthenticatedUserMixin.configure_token_cache(ttl=60)
        assert AuthenticatedUserMixin._token_cache_ttl == 60

    def test_configure_token_cache_disable(self) -> None:
        """Setting TTL to 0 should disable caching."""
        AuthenticatedUserMixin.configure_token_cache(ttl=0)
        assert AuthenticatedUserMixin._token_cache_ttl == 0

    def test_invalidate_cache_specific_token(self) -> None:
        """invalidate_token_cache(token) should remove only that token."""
        AuthenticatedUserMixin._token_cache["tok1"] = ("users:1", time.time() + 300)
        AuthenticatedUserMixin._token_cache["tok2"] = ("users:2", time.time() + 300)

        AuthenticatedUserMixin.invalidate_token_cache("tok1")

        assert "tok1" not in AuthenticatedUserMixin._token_cache
        assert "tok2" in AuthenticatedUserMixin._token_cache

    def test_invalidate_cache_all(self) -> None:
        """invalidate_token_cache() without args should clear all."""
        AuthenticatedUserMixin._token_cache["tok1"] = ("users:1", time.time() + 300)
        AuthenticatedUserMixin._token_cache["tok2"] = ("users:2", time.time() + 300)

        AuthenticatedUserMixin.invalidate_token_cache()

        assert len(AuthenticatedUserMixin._token_cache) == 0

    @pytest.mark.asyncio
    async def test_validate_token_caches_result(self) -> None:
        """validate_token should cache the result on success."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_ephemeral.authenticate = AsyncMock(return_value=AuthResponse(token="tok", success=True, raw={}))
        mock_first_qr = MagicMock(result="TestUser:abc")
        mock_auth_result = MagicMock(first_result=mock_first_qr)
        mock_ephemeral.query = AsyncMock(return_value=mock_auth_result)
        mock_ephemeral.close = AsyncMock()

        with patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)):
            # First call should hit the server
            result = await TestUser.validate_token("my_token")
            assert result == "TestUser:abc"
            assert mock_ephemeral.authenticate.call_count == 1

            # Second call should hit the cache (no new connection)
            mock_ephemeral.authenticate.reset_mock()
            result2 = await TestUser.validate_token("my_token")
            assert result2 == "TestUser:abc"
            mock_ephemeral.authenticate.assert_not_called()

    @pytest.mark.asyncio
    async def test_validate_token_cache_bypass(self) -> None:
        """validate_token(use_cache=False) should bypass the cache."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        # Pre-populate cache
        TestUser._token_cache["my_token"] = ("TestUser:cached", time.time() + 300)

        mock_ephemeral = AsyncMock()
        mock_ephemeral.authenticate = AsyncMock(return_value=AuthResponse(token="tok", success=True, raw={}))
        mock_first_qr = MagicMock(result="TestUser:fresh")
        mock_auth_result = MagicMock(first_result=mock_first_qr)
        mock_ephemeral.query = AsyncMock(return_value=mock_auth_result)
        mock_ephemeral.close = AsyncMock()

        with patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)):
            result = await TestUser.validate_token("my_token", use_cache=False)
            assert result == "TestUser:fresh"
            mock_ephemeral.authenticate.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_token_expired_cache_entry(self) -> None:
        """Expired cache entries should be bypassed."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        # Pre-populate cache with expired entry
        TestUser._token_cache["my_token"] = ("TestUser:old", time.time() - 10)

        mock_ephemeral = AsyncMock()
        mock_ephemeral.authenticate = AsyncMock(return_value=AuthResponse(token="tok", success=True, raw={}))
        mock_first_qr = MagicMock(result="TestUser:new")
        mock_auth_result = MagicMock(first_result=mock_first_qr)
        mock_ephemeral.query = AsyncMock(return_value=mock_auth_result)
        mock_ephemeral.close = AsyncMock()

        with patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)):
            result = await TestUser.validate_token("my_token")
            assert result == "TestUser:new"
            mock_ephemeral.authenticate.assert_called_once()


class TestValidateTokenLocal:
    """Tests for validate_token_local() — local JWT decode."""

    def _make_jwt(self, claims: dict, expired: bool = False) -> str:
        """Build a fake JWT with the given claims."""
        header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=")
        if expired:
            claims["exp"] = int(time.time()) - 3600
        elif "exp" not in claims:
            claims["exp"] = int(time.time()) + 3600
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
        signature = base64.urlsafe_b64encode(b"fake_signature").rstrip(b"=")
        return f"{header.decode()}.{payload.decode()}.{signature.decode()}"

    def test_valid_token_with_id_claim(self) -> None:
        """Should extract record ID from 'ID' claim."""
        token = self._make_jwt({"ID": "users:alice123"})
        result = AuthenticatedUserMixin.validate_token_local(token)
        assert result == "users:alice123"

    def test_valid_token_with_lowercase_id(self) -> None:
        """Should also accept lowercase 'id' claim."""
        token = self._make_jwt({"id": "users:bob456"})
        result = AuthenticatedUserMixin.validate_token_local(token)
        assert result == "users:bob456"

    def test_expired_token(self) -> None:
        """Expired token should return None."""
        token = self._make_jwt({"ID": "users:alice123"}, expired=True)
        result = AuthenticatedUserMixin.validate_token_local(token)
        assert result is None

    def test_no_id_claim(self) -> None:
        """Token without ID claim should return None."""
        token = self._make_jwt({"sub": "something_else"})
        result = AuthenticatedUserMixin.validate_token_local(token)
        assert result is None

    def test_malformed_token(self) -> None:
        """Malformed token should return None."""
        assert AuthenticatedUserMixin.validate_token_local("not.a.valid.jwt.token") is None
        assert AuthenticatedUserMixin.validate_token_local("") is None
        assert AuthenticatedUserMixin.validate_token_local("single_part") is None

    def test_invalid_base64(self) -> None:
        """Invalid base64 in payload should return None."""
        result = AuthenticatedUserMixin.validate_token_local("header.!!!invalid!!!.signature")
        assert result is None

    def test_token_without_exp(self) -> None:
        """Token without exp claim should still work (no expiry check)."""
        claims = {"ID": "users:no_exp"}
        header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
        sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=")
        token = f"{header.decode()}.{payload.decode()}.{sig.decode()}"
        result = AuthenticatedUserMixin.validate_token_local(token)
        assert result == "users:no_exp"


# =============================================================================
# Issue #3: flexible_fields in SurrealConfigDict
# =============================================================================


class TestFlexibleFieldsConfig:
    """flexible_fields in SurrealConfigDict should mark fields as FLEXIBLE."""

    def test_introspector_detects_flexible_fields_from_config(self) -> None:
        """Fields listed in flexible_fields should have flexible=True in FieldState."""

        class GameTable(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="game_tables",
                flexible_fields=["game_state", "current_state"],
            )
            id: str | None = None
            name: str
            game_state: dict | None = None
            current_state: dict | None = None
            status: str = "active"

        introspector = ModelIntrospector(models=[GameTable])
        schema = introspector.introspect()
        table = schema.tables["game_tables"]

        assert table.fields["game_state"].flexible is True
        assert table.fields["current_state"].flexible is True
        assert table.fields["status"].flexible is False
        assert table.fields["name"].flexible is False

    def test_introspector_json_schema_extra_still_works(self) -> None:
        """Field(json_schema_extra={"flexible": True}) should still work."""
        from pydantic import Field

        class FlexModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="flex_model")
            id: str | None = None
            data: dict | None = Field(default=None, json_schema_extra={"flexible": True})

        introspector = ModelIntrospector(models=[FlexModel])
        schema = introspector.introspect()
        table = schema.tables["flex_model"]

        assert table.fields["data"].flexible is True

    def test_introspector_both_methods_combined(self) -> None:
        """Both flexible_fields config and json_schema_extra should work together."""
        from pydantic import Field

        class CombinedModel(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="combined",
                flexible_fields=["config_data"],
            )
            id: str | None = None
            config_data: dict | None = None
            extra_data: dict | None = Field(default=None, json_schema_extra={"flexible": True})
            normal_field: str = ""

        introspector = ModelIntrospector(models=[CombinedModel])
        schema = introspector.introspect()
        table = schema.tables["combined"]

        assert table.fields["config_data"].flexible is True
        assert table.fields["extra_data"].flexible is True
        assert table.fields["normal_field"].flexible is False

    def test_flexible_fields_empty_list(self) -> None:
        """Empty flexible_fields list should not break anything."""

        class NormalModel(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="normal_model",
                flexible_fields=[],
            )
            id: str | None = None
            data: dict | None = None

        introspector = ModelIntrospector(models=[NormalModel])
        schema = introspector.introspect()
        table = schema.tables["normal_model"]

        assert table.fields["data"].flexible is False

    def test_flexible_fields_not_set(self) -> None:
        """Models without flexible_fields should work normally."""

        class PlainModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="plain_model")
            id: str | None = None
            data: dict | None = None

        introspector = ModelIntrospector(models=[PlainModel])
        schema = introspector.introspect()
        table = schema.tables["plain_model"]

        assert table.fields["data"].flexible is False


# =============================================================================
# Issue #4: Large dict parameter binding (regression test)
# =============================================================================


class TestLargeDictCBOREncoding:
    """Large nested dicts should encode/decode correctly via CBOR."""

    def _make_large_game_state(self, num_players: int = 4, hand_size: int = 15) -> dict:
        """Create a realistic game state dict (~20-80KB JSON)."""
        return {
            "phase": "play",
            "round_number": 3,
            "current_player_index": 0,
            "direction": 1,
            "deck": [
                {"suit": s, "rank": r, "id": f"{s}_{r}"}
                for s in ["hearts", "diamonds", "clubs", "spades"]
                for r in ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
            ],
            "discard_pile": [{"suit": "hearts", "rank": str(i), "id": f"discard_{i}"} for i in range(20)],
            "players": [
                {
                    "seat": i,
                    "hand": [{"suit": "clubs", "rank": str(j), "id": f"p{i}_card_{j}"} for j in range(hand_size)],
                    "melds": [
                        {
                            "cards": [{"suit": "hearts", "rank": "7", "id": f"meld_{i}_{k}"} for k in range(4)],
                            "is_canasta": False,
                        }
                        for _ in range(3)
                    ],
                    "score": 1500 + i * 200,
                }
                for i in range(num_players)
            ],
            "team_scores": [3200, 2800],
        }

    def test_large_dict_cbor_roundtrip(self) -> None:
        """Large nested dict should survive CBOR encode/decode."""
        game_state = self._make_large_game_state()
        json_size = len(json.dumps(game_state))
        assert json_size > 10000, f"Expected >10KB, got {json_size}"

        encoded = encode(game_state)
        decoded = decode(encoded)

        assert len(decoded["players"]) == 4
        assert len(decoded["deck"]) == 52
        assert len(decoded["players"][0]["hand"]) == 15
        assert decoded["players"][0]["melds"][0]["cards"][0]["suit"] == "hearts"

    def test_large_dict_with_none_values_cbor_roundtrip(self) -> None:
        """Large dict with scattered None values should roundtrip correctly."""
        game_state = self._make_large_game_state()
        # Add None values throughout the structure
        game_state["metadata"] = None
        game_state["players"][0]["disconnected_at"] = None
        game_state["players"][1]["hand"][0]["special"] = None

        encoded = encode(game_state)
        decoded = decode(encoded)

        assert decoded["metadata"] is None
        assert decoded["players"][0]["disconnected_at"] is None
        assert decoded["players"][1]["hand"][0]["special"] is None
        assert len(decoded["players"]) == 4

    def test_rpc_request_with_large_dict(self) -> None:
        """RPCRequest with large dict params should serialize correctly."""
        game_state = self._make_large_game_state()
        request = RPCRequest(
            method="query",
            params=["UPDATE game:1 SET state = $state", {"state": game_state}],
        )

        # CBOR path
        cbor_data = request.to_cbor()
        assert len(cbor_data) > 1000

        # JSON path
        json_data = request.to_json()
        assert len(json_data) > 1000
        parsed = json.loads(json_data)
        assert parsed["method"] == "query"
