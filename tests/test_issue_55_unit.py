"""
Unit tests for Issue #55 fix: Parameter binding for large nested dicts.

Tests the detection heuristics, inline-dict helpers, and save routing logic.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.utils import _is_complex_value, _SurrealJSONEncoder, inline_dict_variables

# ── _is_complex_value() ─────────────────────────────────────────────────


class TestIsComplexValue:
    """Tests for _is_complex_value() detection heuristic."""

    def test_flat_dict_is_not_complex(self) -> None:
        assert _is_complex_value({"a": 1, "b": "hello", "c": True}) is False

    def test_empty_dict_is_not_complex(self) -> None:
        assert _is_complex_value({}) is False

    def test_dict_with_nested_dict_is_complex(self) -> None:
        assert _is_complex_value({"a": 1, "nested": {"x": 1}}) is True

    def test_dict_with_nested_list_is_complex(self) -> None:
        assert _is_complex_value({"a": 1, "items": [1, 2, 3]}) is True

    def test_dict_with_empty_nested_dict_is_complex(self) -> None:
        # Even an empty nested dict is still a dict value
        assert _is_complex_value({"a": 1, "nested": {}}) is True

    def test_list_with_dicts_is_complex(self) -> None:
        assert _is_complex_value([{"a": 1}, {"b": 2}]) is True

    def test_flat_list_is_not_complex(self) -> None:
        assert _is_complex_value([1, 2, "hello"]) is False

    def test_empty_list_is_not_complex(self) -> None:
        assert _is_complex_value([]) is False

    def test_string_is_not_complex(self) -> None:
        assert _is_complex_value("hello") is False

    def test_int_is_not_complex(self) -> None:
        assert _is_complex_value(42) is False

    def test_none_is_not_complex(self) -> None:
        assert _is_complex_value(None) is False

    def test_game_state_dict_is_complex(self) -> None:
        """Realistic game state dict should be detected as complex."""
        state = {
            "phase": "play",
            "players": [
                {"seat": 0, "hand": [{"suit": "hearts", "rank": "7"}]},
            ],
            "settings": {"max_rounds": 10},
        }
        assert _is_complex_value(state) is True


# ── inline_dict_variables() ──────────────────────────────────────────────


class TestInlineDictVariables:
    """Tests for inline_dict_variables() helper."""

    def test_simple_vars_unchanged(self) -> None:
        """Simple (non-complex) variables should remain in the bindings."""
        query = "SELECT * FROM users WHERE name = $name AND age = $age"
        variables = {"name": "Alice", "age": 30}

        new_query, remaining = inline_dict_variables(query, variables)

        assert new_query == query
        assert remaining == variables

    def test_complex_dict_inlined(self) -> None:
        """Complex dict should be inlined as JSON in the query."""
        query = "UPDATE game:1 SET state = $state"
        state = {"players": [{"name": "Alice"}], "round": 1}
        variables = {"state": state}

        new_query, remaining = inline_dict_variables(query, variables)

        # $state should be replaced with JSON
        assert "$state" not in new_query
        assert '"players"' in new_query
        assert remaining == {}

    def test_mixed_simple_and_complex(self) -> None:
        """Mix of simple and complex variables — only complex ones inlined."""
        query = "UPDATE $table SET state = $state, name = $name"
        variables = {
            "table": "game:1",
            "state": {"nested": {"deep": True}},
            "name": "test",
        }

        new_query, remaining = inline_dict_variables(query, variables)

        assert "$state" not in new_query
        assert "$table" in new_query
        assert "$name" in new_query
        assert remaining == {"table": "game:1", "name": "test"}

    def test_inlined_json_is_valid(self) -> None:
        """Inlined JSON should be parseable."""
        query = "UPDATE x:1 SET data = $data"
        data = {"scores": [100, 200], "meta": {"key": "value"}}
        variables = {"data": data}

        new_query, remaining = inline_dict_variables(query, variables)

        # Extract the JSON from the query
        prefix = "UPDATE x:1 SET data = "
        json_str = new_query[len(prefix) :]
        parsed = json.loads(json_str)
        assert parsed == data

    def test_list_of_dicts_inlined(self) -> None:
        """List containing dicts should be inlined."""
        query = "UPDATE x:1 SET items = $items"
        variables = {"items": [{"a": 1}, {"b": 2}]}

        new_query, remaining = inline_dict_variables(query, variables)

        assert "$items" not in new_query
        assert remaining == {}

    def test_empty_variables(self) -> None:
        """Empty variables dict should be a no-op."""
        query = "SELECT * FROM users"
        new_query, remaining = inline_dict_variables(query, {})
        assert new_query == query
        assert remaining == {}

    def test_original_variables_not_mutated(self) -> None:
        """Original variables dict should not be modified."""
        query = "UPDATE x:1 SET state = $state"
        variables = {"state": {"nested": {"deep": True}}}
        original = dict(variables)

        inline_dict_variables(query, variables)

        assert variables == original

    def test_variable_word_boundary(self) -> None:
        """$var should not match $variable (word boundary)."""
        query = "UPDATE x:1 SET state = $state, state_backup = $state_backup"
        variables = {
            "state": {"nested": {"x": 1}},
            "state_backup": "simple_string",
        }

        new_query, remaining = inline_dict_variables(query, variables)

        assert "$state_backup" in new_query
        assert remaining == {"state_backup": "simple_string"}


# ── _has_complex_nested_data() on BaseSurrealModel ───────────────────────


class TestHasComplexNestedData:
    """Tests for BaseSurrealModel._has_complex_nested_data()."""

    def test_flat_data(self) -> None:
        assert BaseSurrealModel._has_complex_nested_data({"name": "Alice", "age": 30}) is False

    def test_data_with_none(self) -> None:
        assert BaseSurrealModel._has_complex_nested_data({"name": None}) is False

    def test_data_with_nested_dict(self) -> None:
        assert BaseSurrealModel._has_complex_nested_data({"state": {"phase": "play"}}) is False
        # Only complex if the nested dict itself has nested dicts/lists
        assert BaseSurrealModel._has_complex_nested_data({"state": {"nested": {"deep": True}}}) is True

    def test_data_with_list_of_dicts(self) -> None:
        assert BaseSurrealModel._has_complex_nested_data({"items": [{"a": 1}]}) is True

    def test_data_with_flat_list(self) -> None:
        assert BaseSurrealModel._has_complex_nested_data({"tags": [1, 2, 3]}) is False

    def test_data_with_dict_containing_list(self) -> None:
        assert BaseSurrealModel._has_complex_nested_data({"state": {"items": [1, 2]}}) is True


# ── Save routing (mock test) ────────────────────────────────────────────


class MockModel(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="mock_test")
    id: str | None = None
    name: str = "test"
    game_state: dict[str, Any] | None = None


class TestSaveRouting:
    """Verify that _execute_save routes complex data to SET-clause path."""

    @pytest.mark.asyncio
    async def test_complex_data_uses_set_clause_path(self) -> None:
        """Data with nested dicts should route through _execute_save_with_set_clause."""
        model = MockModel(id="1", name="test")
        model._db_persisted = False

        data = {"name": "test", "game_state": {"players": [{"seat": 0}]}}

        with patch.object(model, "_execute_save_with_set_clause", new_callable=AsyncMock) as mock_set:
            await model._execute_save(
                tx=None,
                table="mock_test",
                id="1",
                data=data,
                created=True,
            )
            mock_set.assert_called_once_with(None, "mock_test", "1", data, True, None)

    @pytest.mark.asyncio
    async def test_simple_data_uses_direct_rpc_path(self) -> None:
        """Flat data should use the direct RPC path (upsert/merge/create)."""
        model = MockModel(id="1", name="test")
        model._db_persisted = False

        data = {"name": "test"}

        mock_client = AsyncMock()
        mock_client.upsert = AsyncMock(return_value=MagicMock(exists=True, record={"id": "mock_test:1", "name": "test"}))

        with (
            patch.object(model, "_execute_save_with_set_clause", new_callable=AsyncMock) as mock_set,
            patch(
                "src.surreal_orm.model_base.SurrealDBConnectionManager.get_client",
                new_callable=AsyncMock,
                return_value=mock_client,
            ),
        ):
            await model._execute_save(
                tx=None,
                table="mock_test",
                id="1",
                data=data,
                created=True,
            )
            mock_set.assert_not_called()
            mock_client.upsert.assert_called_once()


# ── _SurrealJSONEncoder ───────────────────────────────────────────────


class TestSurrealJSONEncoder:
    """Tests for _SurrealJSONEncoder used by inline_dict_variables."""

    def test_datetime_serializes_to_isoformat(self) -> None:
        dt = datetime(2026, 2, 15, 10, 30, 0)
        result = json.dumps({"ts": dt}, cls=_SurrealJSONEncoder)
        assert '"2026-02-15T10:30:00"' in result

    def test_date_serializes_to_isoformat(self) -> None:
        d = date(2026, 2, 15)
        result = json.dumps({"d": d}, cls=_SurrealJSONEncoder)
        assert '"2026-02-15"' in result

    def test_time_serializes_to_isoformat(self) -> None:
        t = time(10, 30, 0)
        result = json.dumps({"t": t}, cls=_SurrealJSONEncoder)
        assert '"10:30:00"' in result

    def test_decimal_serializes_to_float(self) -> None:
        val = Decimal("3.14")
        result = json.dumps({"v": val}, cls=_SurrealJSONEncoder)
        parsed = json.loads(result)
        assert parsed["v"] == 3.14

    def test_uuid_serializes_to_string(self) -> None:
        uid = UUID("12345678-1234-5678-1234-567812345678")
        result = json.dumps({"id": uid}, cls=_SurrealJSONEncoder)
        assert '"12345678-1234-5678-1234-567812345678"' in result

    def test_unsupported_type_raises_typeerror(self) -> None:
        with pytest.raises(TypeError):
            json.dumps({"val": object()}, cls=_SurrealJSONEncoder)


# ── inline_dict_variables error handling ──────────────────────────────


class TestInlineDictVariablesErrors:
    """Tests for error handling in inline_dict_variables."""

    def test_non_serializable_value_raises_valueerror(self) -> None:
        """Non-serializable complex values should raise a clear ValueError."""
        query = "UPDATE x:1 SET state = $state"
        variables = {"state": {"nested": {"bad": object()}}}
        with pytest.raises(ValueError, match="Failed to serialize variable 'state'"):
            inline_dict_variables(query, variables)


# ── Connection cache invalidation ─────────────────────────────────────


class TestConnectionCacheInvalidation:
    """Tests for add_connection() cache invalidation."""

    def test_add_connection_invalidates_cached_clients_on_config_change(self) -> None:
        """Changing config should evict cached clients so get_client() reconnects."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        # Set initial config
        SurrealDBConnectionManager.add_connection(
            "test_cache",
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns1",
            database="db1",
        )
        # Simulate a cached client
        sentinel = MagicMock()
        SurrealDBConnectionManager._clients["test_cache"] = sentinel

        # Change config — cached client should be evicted
        SurrealDBConnectionManager.add_connection(
            "test_cache",
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns1",
            database="db2",  # Changed
        )

        assert "test_cache" not in SurrealDBConnectionManager._clients

        # Cleanup
        SurrealDBConnectionManager._configs.pop("test_cache", None)

    def test_add_connection_keeps_cache_when_config_unchanged(self) -> None:
        """Same config should NOT evict cached clients."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        config_kwargs = {
            "url": "http://localhost:8000",
            "user": "root",
            "password": "root",
            "namespace": "ns1",
            "database": "db1",
        }

        SurrealDBConnectionManager.add_connection("test_cache2", **config_kwargs)
        sentinel = MagicMock()
        SurrealDBConnectionManager._clients["test_cache2"] = sentinel

        # Re-add same config — cache should survive
        SurrealDBConnectionManager.add_connection("test_cache2", **config_kwargs)

        assert SurrealDBConnectionManager._clients.get("test_cache2") is sentinel

        # Cleanup
        SurrealDBConnectionManager._configs.pop("test_cache2", None)
        SurrealDBConnectionManager._clients.pop("test_cache2", None)
