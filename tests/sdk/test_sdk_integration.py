"""Comprehensive SDK integration tests for all DB call types.

Tests every RPC method against a real SurrealDB 2.6 instance to ensure
the SDK correctly handles all CRUD operations, auth, transactions, graph
relations, and server functions via both HTTP and WebSocket protocols.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import pytest
from pydantic import BaseModel

from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.exceptions import QueryError
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER, SURREALDB_WS_URL

SURREALDB_DATABASE = "test_sdk_integration"


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(scope="function")
async def http(request: pytest.FixtureRequest) -> AsyncGenerator[HTTPConnection, None]:
    """Create a connected HTTP connection."""
    conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    yield conn
    await conn.close()


@pytest.fixture(scope="function")
async def ws(request: pytest.FixtureRequest) -> AsyncGenerator[WebSocketConnection, None]:
    """Create a connected WebSocket connection."""
    conn = WebSocketConnection(
        SURREALDB_WS_URL,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
        auto_reconnect=False,
    )
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    yield conn
    await conn.close()


# ── Helper ──────────────────────────────────────────────────────────────


async def cleanup(conn: HTTPConnection | WebSocketConnection, *tables: str) -> None:
    """Remove test tables."""
    for t in tables:
        await conn.query(f"DELETE {t}")


# ═══════════════════════════════════════════════════════════════════════
# HTTP Connection — All DB Call Types
# ═══════════════════════════════════════════════════════════════════════


class TestHTTPAuth:
    """HTTP authentication tests."""

    @pytest.mark.integration
    async def test_signin_root(self) -> None:
        """Root signin returns token and sets auth state."""
        conn = HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE)
        await conn.connect()
        try:
            resp = await conn.signin(SURREALDB_USER, SURREALDB_PASS)
            assert resp.success
            assert conn.is_authenticated
            assert conn.token is not None
        finally:
            await conn.close()

    @pytest.mark.integration
    async def test_version(self, http: HTTPConnection) -> None:
        """Version returns a non-empty string."""
        v = await http.version()
        assert isinstance(v, str)
        assert "surrealdb" in v.lower() or len(v) > 0

    @pytest.mark.integration
    async def test_ping(self, http: HTTPConnection) -> None:
        """Ping returns True on live connection."""
        assert await http.ping() is True

    @pytest.mark.integration
    async def test_health(self, http: HTTPConnection) -> None:
        """Health check on live connection."""
        result = await http.health()
        assert result is True


class TestHTTPQuery:
    """HTTP query execution tests."""

    @pytest.mark.integration
    async def test_query_info(self, http: HTTPConnection) -> None:
        """Execute INFO FOR DB query."""
        result = await http.query("INFO FOR DB")
        assert result.is_ok

    @pytest.mark.integration
    async def test_query_return_scalar(self, http: HTTPConnection) -> None:
        """Query returning a scalar value."""
        result = await http.query("RETURN 42")
        assert result.is_ok
        assert result.first_result is not None
        assert result.first_result.result == 42

    @pytest.mark.integration
    async def test_query_return_string(self, http: HTTPConnection) -> None:
        """Query returning a string."""
        result = await http.query("RETURN 'hello'")
        assert result.is_ok
        assert result.first_result is not None
        assert result.first_result.result == "hello"

    @pytest.mark.integration
    async def test_query_with_variables(self, http: HTTPConnection) -> None:
        """Query with parameterized variables."""
        result = await http.query("RETURN $x + $y", {"x": 10, "y": 20})
        assert result.is_ok
        assert result.first_result is not None
        assert result.first_result.result == 30

    @pytest.mark.integration
    async def test_query_multi_statement(self, http: HTTPConnection) -> None:
        """Query with multiple statements."""
        result = await http.query("RETURN 1; RETURN 2; RETURN 3;")
        assert result.is_ok
        assert len(result.results) == 3

    @pytest.mark.integration
    async def test_query_select_from_table(self, http: HTTPConnection) -> None:
        """SELECT query on a table."""
        await cleanup(http, "sdk_query_test")
        await http.query("CREATE sdk_query_test:1 SET name = 'Alice'")
        await http.query("CREATE sdk_query_test:2 SET name = 'Bob'")

        result = await http.query("SELECT * FROM sdk_query_test ORDER BY name")
        assert result.is_ok
        records = result.all_records
        assert len(records) == 2
        assert records[0]["name"] == "Alice"
        assert records[1]["name"] == "Bob"

        await cleanup(http, "sdk_query_test")


class TestHTTPCreate:
    """HTTP create operation tests."""

    @pytest.mark.integration
    async def test_create_with_table(self, http: HTTPConnection) -> None:
        """Create a record on a table (auto-generated ID)."""
        await cleanup(http, "sdk_create_test")

        resp = await http.create("sdk_create_test", {"name": "Alice", "age": 30})
        assert resp.exists
        assert resp.record is not None
        assert resp.record["name"] == "Alice"
        assert resp.record["age"] == 30
        assert "id" in resp.record

        await cleanup(http, "sdk_create_test")

    @pytest.mark.integration
    async def test_create_with_specific_id(self, http: HTTPConnection) -> None:
        """Create a record with a specific ID."""
        await cleanup(http, "sdk_create_test")

        resp = await http.create("sdk_create_test:alice", {"name": "Alice", "age": 30})
        assert resp.exists
        assert resp.id is not None
        assert "alice" in resp.id

        await cleanup(http, "sdk_create_test")

    @pytest.mark.integration
    async def test_create_empty_data(self, http: HTTPConnection) -> None:
        """Create a record with no data."""
        await cleanup(http, "sdk_create_empty")

        resp = await http.create("sdk_create_empty:1", {})
        assert resp.exists
        assert resp.id is not None

        await cleanup(http, "sdk_create_empty")


class TestHTTPSelect:
    """HTTP select operation tests."""

    @pytest.mark.integration
    async def test_select_table(self, http: HTTPConnection) -> None:
        """Select all records from a table."""
        await cleanup(http, "sdk_select_test")
        await http.create("sdk_select_test:1", {"name": "Alice"})
        await http.create("sdk_select_test:2", {"name": "Bob"})

        resp = await http.select("sdk_select_test")
        assert resp.count == 2

        await cleanup(http, "sdk_select_test")

    @pytest.mark.integration
    async def test_select_specific_record(self, http: HTTPConnection) -> None:
        """Select a specific record by ID."""
        await cleanup(http, "sdk_select_test")
        await http.create("sdk_select_test:alice", {"name": "Alice", "age": 30})

        resp = await http.select("sdk_select_test:alice")
        assert resp.count == 1
        assert resp.first is not None
        assert resp.first["name"] == "Alice"

        await cleanup(http, "sdk_select_test")

    @pytest.mark.integration
    async def test_select_empty_table(self, http: HTTPConnection) -> None:
        """Select from a table with no records."""
        await cleanup(http, "sdk_select_empty")

        resp = await http.select("sdk_select_empty")
        assert resp.is_empty

        await cleanup(http, "sdk_select_empty")


class TestHTTPInsert:
    """HTTP insert operation tests."""

    @pytest.mark.integration
    async def test_insert_single(self, http: HTTPConnection) -> None:
        """Insert a single record."""
        await cleanup(http, "sdk_insert_test")

        resp = await http.insert("sdk_insert_test", {"id": "1", "name": "Alice"})
        assert resp.count == 1

        await cleanup(http, "sdk_insert_test")

    @pytest.mark.integration
    async def test_insert_batch(self, http: HTTPConnection) -> None:
        """Insert multiple records at once."""
        await cleanup(http, "sdk_insert_test")

        resp = await http.insert(
            "sdk_insert_test",
            [
                {"id": "1", "name": "Alice"},
                {"id": "2", "name": "Bob"},
                {"id": "3", "name": "Carol"},
            ],
        )
        assert resp.count == 3

        await cleanup(http, "sdk_insert_test")


class TestHTTPUpdate:
    """HTTP update operation tests."""

    @pytest.mark.integration
    async def test_update_specific_record(self, http: HTTPConnection) -> None:
        """Update replaces all fields on a specific record."""
        await cleanup(http, "sdk_update_test")
        await http.create("sdk_update_test:1", {"name": "Alice", "age": 30, "role": "user"})

        resp = await http.update("sdk_update_test:1", {"name": "Alice Updated", "age": 31})
        assert resp.count >= 1
        record = resp.first
        assert record is not None
        assert record["name"] == "Alice Updated"
        assert record["age"] == 31
        # role should be gone (update replaces all fields)
        assert record.get("role") is None

        await cleanup(http, "sdk_update_test")


class TestHTTPUpsert:
    """HTTP upsert operation tests."""

    @pytest.mark.integration
    async def test_upsert_create_new(self, http: HTTPConnection) -> None:
        """Upsert creates a new record when it doesn't exist."""
        await cleanup(http, "sdk_upsert_test")

        resp = await http.upsert("sdk_upsert_test:1", {"name": "Alice", "age": 30})
        assert resp.count >= 1
        assert resp.first is not None
        assert resp.first["name"] == "Alice"

        await cleanup(http, "sdk_upsert_test")

    @pytest.mark.integration
    async def test_upsert_update_existing(self, http: HTTPConnection) -> None:
        """Upsert updates an existing record."""
        await cleanup(http, "sdk_upsert_test")
        await http.create("sdk_upsert_test:1", {"name": "Alice", "age": 30})

        resp = await http.upsert("sdk_upsert_test:1", {"name": "Alice", "age": 31})
        assert resp.count >= 1
        assert resp.first is not None
        assert resp.first["age"] == 31

        # Verify only one record
        all_resp = await http.select("sdk_upsert_test")
        assert all_resp.count == 1

        await cleanup(http, "sdk_upsert_test")


class TestHTTPMerge:
    """HTTP merge operation tests."""

    @pytest.mark.integration
    async def test_merge_partial_update(self, http: HTTPConnection) -> None:
        """Merge only updates specified fields, preserving others."""
        await cleanup(http, "sdk_merge_test")
        await http.create("sdk_merge_test:1", {"name": "Alice", "age": 30, "role": "user"})

        resp = await http.merge("sdk_merge_test:1", {"age": 31})
        assert resp.count >= 1
        record = resp.first
        assert record is not None
        assert record["age"] == 31
        assert record["name"] == "Alice"  # preserved
        assert record["role"] == "user"  # preserved

        await cleanup(http, "sdk_merge_test")

    @pytest.mark.integration
    async def test_merge_add_new_field(self, http: HTTPConnection) -> None:
        """Merge can add new fields to existing record."""
        await cleanup(http, "sdk_merge_test")
        await http.create("sdk_merge_test:1", {"name": "Alice"})

        resp = await http.merge("sdk_merge_test:1", {"email": "alice@example.com"})
        assert resp.count >= 1
        record = resp.first
        assert record is not None
        assert record["name"] == "Alice"
        assert record["email"] == "alice@example.com"

        await cleanup(http, "sdk_merge_test")


class TestHTTPPatch:
    """HTTP JSON Patch operation tests."""

    @pytest.mark.integration
    async def test_patch_replace(self, http: HTTPConnection) -> None:
        """Patch with replace operation."""
        await cleanup(http, "sdk_patch_test")
        await http.create("sdk_patch_test:1", {"name": "Alice", "age": 30})

        resp = await http.patch(
            "sdk_patch_test:1",
            [{"op": "replace", "path": "/age", "value": 31}],
        )
        assert resp.count >= 1
        record = resp.first
        assert record is not None
        assert record["age"] == 31
        assert record["name"] == "Alice"

        await cleanup(http, "sdk_patch_test")

    @pytest.mark.integration
    async def test_patch_add(self, http: HTTPConnection) -> None:
        """Patch with add operation."""
        await cleanup(http, "sdk_patch_test")
        await http.create("sdk_patch_test:1", {"name": "Alice"})

        resp = await http.patch(
            "sdk_patch_test:1",
            [{"op": "add", "path": "/email", "value": "alice@example.com"}],
        )
        assert resp.count >= 1
        record = resp.first
        assert record is not None
        assert record["email"] == "alice@example.com"

        await cleanup(http, "sdk_patch_test")

    @pytest.mark.integration
    async def test_patch_remove(self, http: HTTPConnection) -> None:
        """Patch with remove operation."""
        await cleanup(http, "sdk_patch_test")
        await http.create("sdk_patch_test:1", {"name": "Alice", "temp": "remove_me"})

        resp = await http.patch(
            "sdk_patch_test:1",
            [{"op": "remove", "path": "/temp"}],
        )
        assert resp.count >= 1
        record = resp.first
        assert record is not None
        assert "temp" not in record

        await cleanup(http, "sdk_patch_test")


class TestHTTPDelete:
    """HTTP delete operation tests."""

    @pytest.mark.integration
    async def test_delete_specific_record(self, http: HTTPConnection) -> None:
        """Delete a specific record by ID."""
        await cleanup(http, "sdk_delete_test")
        await http.create("sdk_delete_test:1", {"name": "Alice"})
        await http.create("sdk_delete_test:2", {"name": "Bob"})

        resp = await http.delete("sdk_delete_test:1")
        assert resp.count == 1

        # Verify only Bob remains
        remaining = await http.select("sdk_delete_test")
        assert remaining.count == 1
        assert remaining.first is not None
        assert remaining.first["name"] == "Bob"

        await cleanup(http, "sdk_delete_test")

    @pytest.mark.integration
    async def test_delete_all_records(self, http: HTTPConnection) -> None:
        """Delete all records from a table."""
        await cleanup(http, "sdk_delete_test")
        await http.create("sdk_delete_test:1", {"name": "Alice"})
        await http.create("sdk_delete_test:2", {"name": "Bob"})

        resp = await http.delete("sdk_delete_test")
        assert resp.count == 2

        remaining = await http.select("sdk_delete_test")
        assert remaining.is_empty

        await cleanup(http, "sdk_delete_test")


class TestHTTPRelate:
    """HTTP graph relation tests."""

    @pytest.mark.integration
    async def test_relate_basic(self, http: HTTPConnection) -> None:
        """Create a graph relation between two records."""
        await cleanup(http, "sdk_person", "sdk_follows")
        await http.create("sdk_person:alice", {"name": "Alice"})
        await http.create("sdk_person:bob", {"name": "Bob"})

        resp = await http.relate("sdk_person:alice", "sdk_follows", "sdk_person:bob")
        assert resp.exists
        assert resp.record is not None
        assert "id" in resp.record

        # Verify via graph traversal query
        result = await http.query("SELECT ->sdk_follows->sdk_person.name AS following FROM sdk_person:alice")
        assert result.is_ok

        await cleanup(http, "sdk_person", "sdk_follows")

    @pytest.mark.integration
    async def test_relate_with_data(self, http: HTTPConnection) -> None:
        """Create a relation with edge data."""
        await cleanup(http, "sdk_user", "sdk_likes")
        await http.create("sdk_user:alice", {"name": "Alice"})
        await http.create("sdk_user:bob", {"name": "Bob"})

        resp = await http.relate(
            "sdk_user:alice",
            "sdk_likes",
            "sdk_user:bob",
            data={"since": "2026-01-01", "strength": 5},
        )
        assert resp.exists
        assert resp.record is not None
        assert resp.record.get("since") == "2026-01-01"
        assert resp.record.get("strength") == 5

        await cleanup(http, "sdk_user", "sdk_likes")


class TestHTTPTransactions:
    """HTTP transaction tests."""

    @pytest.mark.integration
    async def test_transaction_commit(self, http: HTTPConnection) -> None:
        """Transaction commit persists all changes."""
        await cleanup(http, "sdk_tx_test")

        async with http.transaction() as tx:
            await tx.create("sdk_tx_test:1", {"name": "Alice", "balance": 100})
            await tx.create("sdk_tx_test:2", {"name": "Bob", "balance": 200})

        result = await http.query("SELECT * FROM sdk_tx_test ORDER BY name")
        records = result.all_records
        assert len(records) == 2
        assert records[0]["name"] == "Alice"
        assert records[1]["name"] == "Bob"

        await cleanup(http, "sdk_tx_test")

    @pytest.mark.integration
    async def test_transaction_rollback(self, http: HTTPConnection) -> None:
        """Transaction rollback discards all changes."""
        await cleanup(http, "sdk_tx_test")

        try:
            async with http.transaction() as tx:
                await tx.create("sdk_tx_test:1", {"name": "Alice"})
                raise ValueError("Force rollback")
        except ValueError:
            pass

        result = await http.query("SELECT * FROM sdk_tx_test")
        assert len(result.all_records) == 0

    @pytest.mark.integration
    async def test_transaction_mixed_ops(self, http: HTTPConnection) -> None:
        """Transaction with create, update, and delete."""
        await cleanup(http, "sdk_tx_mixed")
        # Pre-create a record to update and one to delete
        await http.create("sdk_tx_mixed:existing", {"name": "Existing", "status": "old"})
        await http.create("sdk_tx_mixed:to_delete", {"name": "DeleteMe"})

        async with http.transaction() as tx:
            await tx.create("sdk_tx_mixed:new", {"name": "New"})
            await tx.update("sdk_tx_mixed:existing", {"name": "Existing", "status": "updated"})
            await tx.delete("sdk_tx_mixed:to_delete")

        result = await http.query("SELECT * FROM sdk_tx_mixed ORDER BY name")
        records = result.all_records
        assert len(records) == 2
        names = {r["name"] for r in records}
        assert "Existing" in names
        assert "New" in names
        assert "DeleteMe" not in names

        # Verify update applied
        existing = [r for r in records if r["name"] == "Existing"][0]
        assert existing["status"] == "updated"

        await cleanup(http, "sdk_tx_mixed")


class TestHTTPFunctions:
    """HTTP function call tests."""

    @pytest.mark.integration
    async def test_fn_math(self, http: HTTPConnection) -> None:
        """Built-in math functions."""
        assert await http.fn.math.sqrt(16) == 4.0
        assert await http.fn.math.abs(-42) == 42

    @pytest.mark.integration
    async def test_fn_string(self, http: HTTPConnection) -> None:
        """Built-in string functions."""
        assert await http.fn.string.lowercase("HELLO") == "hello"
        assert await http.fn.string.uppercase("hello") == "HELLO"
        assert await http.fn.string.len("hello") == 5

    @pytest.mark.integration
    async def test_fn_array(self, http: HTTPConnection) -> None:
        """Built-in array functions."""
        assert await http.fn.array.len([1, 2, 3]) == 3

    @pytest.mark.integration
    async def test_fn_time_now(self, http: HTTPConnection) -> None:
        """time::now() returns a value."""
        result = await http.fn.time.now()
        assert result is not None

    @pytest.mark.integration
    async def test_fn_crypto_sha256(self, http: HTTPConnection) -> None:
        """crypto::sha256 returns 64-char hex string."""
        result = await http.fn.crypto.sha256("test")
        assert isinstance(result, str)
        assert len(result) == 64

    @pytest.mark.integration
    async def test_call_with_pydantic_return(self, http: HTTPConnection) -> None:
        """call() with Pydantic return type conversion."""

        class MathResult(BaseModel):
            value: float

        # Define a simple function
        await http.query("""
            DEFINE FUNCTION fn::sdk_test_add($a: int, $b: int) {
                RETURN { value: $a + $b };
            };
        """)

        result = await http.call(
            "sdk_test_add",
            params={"a": 10, "b": 20},
            return_type=MathResult,
        )
        assert isinstance(result, MathResult)
        assert result.value == 30

        await http.query("REMOVE FUNCTION fn::sdk_test_add")

    @pytest.mark.integration
    async def test_call_with_dataclass_return(self, http: HTTPConnection) -> None:
        """call() with dataclass return type conversion."""

        @dataclass
        class CountResult:
            total: int

        await http.query("""
            DEFINE FUNCTION fn::sdk_test_count($items: array) {
                RETURN { total: array::len($items) };
            };
        """)

        result = await http.call(
            "sdk_test_count",
            params={"items": [1, 2, 3, 4, 5]},
            return_type=CountResult,
        )
        assert isinstance(result, CountResult)
        assert result.total == 5

        await http.query("REMOVE FUNCTION fn::sdk_test_count")


class TestHTTPUseNamespace:
    """HTTP namespace/database switching tests."""

    @pytest.mark.integration
    async def test_use_switches_namespace(self, http: HTTPConnection) -> None:
        """use() changes the active namespace and database."""
        original_ns = http.namespace
        original_db = http.database

        await http.use("test", "test_sdk_other_db")
        assert http.namespace == "test"
        assert http.database == "test_sdk_other_db"

        # Switch back
        await http.use(original_ns, original_db)
        assert http.namespace == original_ns
        assert http.database == original_db


class TestHTTPDataTypes:
    """HTTP data type round-trip tests."""

    @pytest.mark.integration
    async def test_string_roundtrip(self, http: HTTPConnection) -> None:
        """Strings survive a create/select round-trip."""
        await cleanup(http, "sdk_types_test")
        await http.create("sdk_types_test:1", {"value": "hello world"})
        resp = await http.select("sdk_types_test:1")
        assert resp.first is not None
        assert resp.first["value"] == "hello world"
        await cleanup(http, "sdk_types_test")

    @pytest.mark.integration
    async def test_int_roundtrip(self, http: HTTPConnection) -> None:
        """Integers survive a create/select round-trip."""
        await cleanup(http, "sdk_types_test")
        await http.create("sdk_types_test:1", {"value": 42})
        resp = await http.select("sdk_types_test:1")
        assert resp.first is not None
        assert resp.first["value"] == 42
        await cleanup(http, "sdk_types_test")

    @pytest.mark.integration
    async def test_float_roundtrip(self, http: HTTPConnection) -> None:
        """Floats survive a create/select round-trip."""
        await cleanup(http, "sdk_types_test")
        await http.create("sdk_types_test:1", {"value": 3.14})
        resp = await http.select("sdk_types_test:1")
        assert resp.first is not None
        assert abs(resp.first["value"] - 3.14) < 0.001
        await cleanup(http, "sdk_types_test")

    @pytest.mark.integration
    async def test_bool_roundtrip(self, http: HTTPConnection) -> None:
        """Booleans survive a create/select round-trip."""
        await cleanup(http, "sdk_types_test")
        await http.create("sdk_types_test:1", {"value": True})
        resp = await http.select("sdk_types_test:1")
        assert resp.first is not None
        assert resp.first["value"] is True
        await cleanup(http, "sdk_types_test")

    @pytest.mark.integration
    async def test_array_roundtrip(self, http: HTTPConnection) -> None:
        """Arrays survive a create/select round-trip."""
        await cleanup(http, "sdk_types_test")
        await http.create("sdk_types_test:1", {"value": [1, "two", 3.0, True]})
        resp = await http.select("sdk_types_test:1")
        assert resp.first is not None
        assert resp.first["value"] == [1, "two", 3.0, True]
        await cleanup(http, "sdk_types_test")

    @pytest.mark.integration
    async def test_nested_object_roundtrip(self, http: HTTPConnection) -> None:
        """Nested objects survive a create/select round-trip."""
        await cleanup(http, "sdk_types_test")
        data = {"address": {"street": "123 Main St", "city": "NYC"}, "tags": ["a", "b"]}
        await http.create("sdk_types_test:1", data)
        resp = await http.select("sdk_types_test:1")
        assert resp.first is not None
        assert resp.first["address"]["city"] == "NYC"
        assert resp.first["tags"] == ["a", "b"]
        await cleanup(http, "sdk_types_test")

    @pytest.mark.integration
    async def test_null_roundtrip(self, http: HTTPConnection) -> None:
        """None values are sent as NONE (absent) via CBOR, not NULL.

        Since v0.14.2, Python None is encoded as CBORTag(TAG_NONE) which
        SurrealDB treats as NONE (absent field), not NULL.  On SCHEMAFULL
        tables with ``option<T>`` this is required — NULL is rejected but
        NONE is accepted.
        """
        await cleanup(http, "sdk_types_test")
        await http.create("sdk_types_test:1", {"value": None})
        resp = await http.select("sdk_types_test:1")
        assert resp.first is not None
        # Field is absent (NONE) — not stored as NULL
        assert "value" not in resp.first
        await cleanup(http, "sdk_types_test")

    @pytest.mark.integration
    async def test_data_url_string(self, http: HTTPConnection) -> None:
        """data: URL strings are preserved (not treated as record links)."""
        await cleanup(http, "sdk_types_test")
        data_url = "data:image/png;base64,iVBORw0KGgo="
        await http.create("sdk_types_test:1", {"avatar": data_url})
        resp = await http.select("sdk_types_test:1")
        assert resp.first is not None
        assert resp.first["avatar"] == data_url
        await cleanup(http, "sdk_types_test")


# ═══════════════════════════════════════════════════════════════════════
# WebSocket Connection — All DB Call Types
# ═══════════════════════════════════════════════════════════════════════


class TestWSAuth:
    """WebSocket authentication tests."""

    @pytest.mark.integration
    async def test_signin_root(self) -> None:
        """Root signin via WebSocket."""
        conn = WebSocketConnection(SURREALDB_WS_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, auto_reconnect=False)
        await conn.connect()
        try:
            resp = await conn.signin(SURREALDB_USER, SURREALDB_PASS)
            assert resp.success
            assert conn.is_authenticated
        finally:
            await conn.close()

    @pytest.mark.integration
    async def test_version(self, ws: WebSocketConnection) -> None:
        """Version via WebSocket."""
        v = await ws.version()
        assert isinstance(v, str)
        assert len(v) > 0

    @pytest.mark.integration
    async def test_ping(self, ws: WebSocketConnection) -> None:
        """Ping via WebSocket."""
        assert await ws.ping() is True


class TestWSQuery:
    """WebSocket query execution tests."""

    @pytest.mark.integration
    async def test_query_return_scalar(self, ws: WebSocketConnection) -> None:
        """Query returning a scalar via WebSocket."""
        result = await ws.query("RETURN 42")
        assert result.is_ok
        assert result.first_result is not None
        assert result.first_result.result == 42

    @pytest.mark.integration
    async def test_query_with_variables(self, ws: WebSocketConnection) -> None:
        """Query with parameterized variables via WebSocket."""
        result = await ws.query("RETURN $x + $y", {"x": 10, "y": 20})
        assert result.is_ok
        assert result.first_result is not None
        assert result.first_result.result == 30

    @pytest.mark.integration
    async def test_session_variables(self, ws: WebSocketConnection) -> None:
        """Let/unset session variables."""
        await ws.let("my_var", 42)
        result = await ws.query("RETURN $my_var")
        assert result.is_ok
        assert result.first_result is not None
        assert result.first_result.result == 42
        await ws.unset("my_var")


class TestWSCRUD:
    """WebSocket CRUD operation tests."""

    @pytest.mark.integration
    async def test_create(self, ws: WebSocketConnection) -> None:
        """Create via WebSocket."""
        await cleanup(ws, "sdk_ws_test")

        resp = await ws.create("sdk_ws_test:1", {"name": "Alice", "age": 30})
        assert resp.exists
        assert resp.record is not None
        assert resp.record["name"] == "Alice"

        await cleanup(ws, "sdk_ws_test")

    @pytest.mark.integration
    async def test_select(self, ws: WebSocketConnection) -> None:
        """Select via WebSocket."""
        await cleanup(ws, "sdk_ws_test")
        await ws.create("sdk_ws_test:1", {"name": "Alice"})
        await ws.create("sdk_ws_test:2", {"name": "Bob"})

        resp = await ws.select("sdk_ws_test")
        assert resp.count == 2

        await cleanup(ws, "sdk_ws_test")

    @pytest.mark.integration
    async def test_insert(self, ws: WebSocketConnection) -> None:
        """Insert via WebSocket."""
        await cleanup(ws, "sdk_ws_test")

        resp = await ws.insert(
            "sdk_ws_test",
            [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}],
        )
        assert resp.count == 2

        await cleanup(ws, "sdk_ws_test")

    @pytest.mark.integration
    async def test_update(self, ws: WebSocketConnection) -> None:
        """Update (full replace) via WebSocket."""
        await cleanup(ws, "sdk_ws_test")
        await ws.create("sdk_ws_test:1", {"name": "Alice", "age": 30, "role": "user"})

        resp = await ws.update("sdk_ws_test:1", {"name": "Alice Updated", "age": 31})
        assert resp.count >= 1
        record = resp.first
        assert record is not None
        assert record["name"] == "Alice Updated"
        assert record.get("role") is None  # replaced

        await cleanup(ws, "sdk_ws_test")

    @pytest.mark.integration
    async def test_upsert(self, ws: WebSocketConnection) -> None:
        """Upsert via WebSocket."""
        await cleanup(ws, "sdk_ws_test")

        # Create via upsert
        resp = await ws.upsert("sdk_ws_test:1", {"name": "Alice", "age": 30})
        assert resp.count >= 1

        # Update via upsert
        resp = await ws.upsert("sdk_ws_test:1", {"name": "Alice", "age": 31})
        assert resp.count >= 1
        assert resp.first is not None
        assert resp.first["age"] == 31

        # Only one record
        all_resp = await ws.select("sdk_ws_test")
        assert all_resp.count == 1

        await cleanup(ws, "sdk_ws_test")

    @pytest.mark.integration
    async def test_merge(self, ws: WebSocketConnection) -> None:
        """Merge (partial update) via WebSocket."""
        await cleanup(ws, "sdk_ws_test")
        await ws.create("sdk_ws_test:1", {"name": "Alice", "age": 30, "role": "user"})

        resp = await ws.merge("sdk_ws_test:1", {"age": 31})
        assert resp.count >= 1
        record = resp.first
        assert record is not None
        assert record["age"] == 31
        assert record["name"] == "Alice"  # preserved
        assert record["role"] == "user"  # preserved

        await cleanup(ws, "sdk_ws_test")

    @pytest.mark.integration
    async def test_patch(self, ws: WebSocketConnection) -> None:
        """JSON Patch via WebSocket."""
        await cleanup(ws, "sdk_ws_test")
        await ws.create("sdk_ws_test:1", {"name": "Alice", "age": 30})

        resp = await ws.patch(
            "sdk_ws_test:1",
            [{"op": "replace", "path": "/age", "value": 31}],
        )
        assert resp.count >= 1
        assert resp.first is not None
        assert resp.first["age"] == 31

        await cleanup(ws, "sdk_ws_test")

    @pytest.mark.integration
    async def test_delete(self, ws: WebSocketConnection) -> None:
        """Delete via WebSocket."""
        await cleanup(ws, "sdk_ws_test")
        await ws.create("sdk_ws_test:1", {"name": "Alice"})
        await ws.create("sdk_ws_test:2", {"name": "Bob"})

        resp = await ws.delete("sdk_ws_test:1")
        assert resp.count == 1

        remaining = await ws.select("sdk_ws_test")
        assert remaining.count == 1

        await cleanup(ws, "sdk_ws_test")

    @pytest.mark.integration
    async def test_relate(self, ws: WebSocketConnection) -> None:
        """Graph relation via WebSocket."""
        await cleanup(ws, "sdk_ws_person", "sdk_ws_follows")
        await ws.create("sdk_ws_person:alice", {"name": "Alice"})
        await ws.create("sdk_ws_person:bob", {"name": "Bob"})

        resp = await ws.relate("sdk_ws_person:alice", "sdk_ws_follows", "sdk_ws_person:bob")
        assert resp.exists

        await cleanup(ws, "sdk_ws_person", "sdk_ws_follows")


class TestWSTransactions:
    """WebSocket transaction tests."""

    @pytest.mark.integration
    async def test_transaction_commit(self, ws: WebSocketConnection) -> None:
        """WebSocket transaction commit."""
        await cleanup(ws, "sdk_ws_tx")

        async with ws.transaction() as tx:
            await tx.create("sdk_ws_tx:1", {"name": "Alice"})
            await tx.create("sdk_ws_tx:2", {"name": "Bob"})

        result = await ws.query("SELECT * FROM sdk_ws_tx")
        assert len(result.all_records) == 2

        await cleanup(ws, "sdk_ws_tx")

    @pytest.mark.integration
    async def test_transaction_rollback_sends_cancel(self, ws: WebSocketConnection) -> None:
        """WebSocket transaction rollback sends CANCEL TRANSACTION.

        Note: SurrealDB 2.6 does not support multi-RPC-call transactions
        over WebSocket. Each RPC `query()` call is treated independently,
        so BEGIN/CREATE/CANCEL sent as separate RPCs won't form an atomic
        transaction. The HTTP transaction works because it batches all
        statements into a single query. This test verifies the rollback
        mechanism is invoked (the tx is marked rolled back) even though
        the DB won't actually undo the CREATE.
        """
        await cleanup(ws, "sdk_ws_tx")

        try:
            async with ws.transaction() as tx:
                await tx.create("sdk_ws_tx:1", {"name": "Alice"})
                raise ValueError("Force rollback")
        except ValueError:
            pass

        # Verify tx state shows rollback was attempted
        assert tx.is_rolled_back
        assert not tx.is_active

        await cleanup(ws, "sdk_ws_tx")


class TestWSLiveQueries:
    """WebSocket live query tests."""

    @pytest.mark.integration
    async def test_live_subscribe_and_kill(self, ws: WebSocketConnection) -> None:
        """Subscribe to live query and kill it."""
        notifications: list[Any] = []

        async def callback(data: Any) -> None:
            notifications.append(data)

        live_id = await ws.live("sdk_ws_live_test", callback)
        assert live_id is not None
        assert live_id in ws.live_queries

        await ws.kill(live_id)
        assert live_id not in ws.live_queries

        await cleanup(ws, "sdk_ws_live_test")


class TestWSFunctions:
    """WebSocket function call tests."""

    @pytest.mark.integration
    async def test_fn_math(self, ws: WebSocketConnection) -> None:
        """Built-in math functions via WebSocket."""
        assert await ws.fn.math.sqrt(25) == 5.0
        assert await ws.fn.math.abs(-10) == 10

    @pytest.mark.integration
    async def test_fn_string(self, ws: WebSocketConnection) -> None:
        """Built-in string functions via WebSocket."""
        assert await ws.fn.string.lowercase("HELLO") == "hello"
        assert await ws.fn.string.len("test") == 4


class TestWSUseNamespace:
    """WebSocket namespace switching tests."""

    @pytest.mark.integration
    async def test_use_switches_namespace(self, ws: WebSocketConnection) -> None:
        """use() via WebSocket."""
        original_ns = ws.namespace
        original_db = ws.database

        await ws.use("test", "test_sdk_ws_other_db")
        assert ws.namespace == "test"
        assert ws.database == "test_sdk_ws_other_db"

        await ws.use(original_ns, original_db)


# ═══════════════════════════════════════════════════════════════════════
# Cross-Protocol Consistency
# ═══════════════════════════════════════════════════════════════════════


class TestProtocolConsistency:
    """Ensure HTTP and WebSocket return consistent results."""

    @pytest.mark.integration
    async def test_create_select_consistency(self, http: HTTPConnection, ws: WebSocketConnection) -> None:
        """Record created via HTTP should be readable via WS and vice versa."""
        await cleanup(http, "sdk_consistency_test")

        # Create via HTTP
        await http.create("sdk_consistency_test:http1", {"name": "HTTP Alice", "source": "http"})

        # Create via WS
        await ws.create("sdk_consistency_test:ws1", {"name": "WS Bob", "source": "ws"})

        # Read both from HTTP
        http_resp = await http.select("sdk_consistency_test")
        assert http_resp.count == 2

        # Read both from WS
        ws_resp = await ws.select("sdk_consistency_test")
        assert ws_resp.count == 2

        await cleanup(http, "sdk_consistency_test")

    @pytest.mark.integration
    async def test_query_result_format(self, http: HTTPConnection, ws: WebSocketConnection) -> None:
        """Both protocols return results in the same format."""
        http_result = await http.query("RETURN 42")
        ws_result = await ws.query("RETURN 42")

        assert http_result.first_result is not None
        assert ws_result.first_result is not None
        assert http_result.first_result.result == ws_result.first_result.result == 42


# ═══════════════════════════════════════════════════════════════════════
# Error Handling
# ═══════════════════════════════════════════════════════════════════════


class TestHTTPErrors:
    """HTTP error handling tests."""

    @pytest.mark.integration
    async def test_invalid_query_raises(self, http: HTTPConnection) -> None:
        """Invalid SurrealQL raises QueryError."""
        with pytest.raises(QueryError):
            await http.query("INVALID QUERY SYNTAX !!!")

    @pytest.mark.integration
    async def test_select_nonexistent_record(self, http: HTTPConnection) -> None:
        """Selecting a nonexistent record returns empty."""
        resp = await http.select("nonexistent_table_12345:nonexistent_id")
        assert resp.is_empty


class TestWSErrors:
    """WebSocket error handling tests."""

    @pytest.mark.integration
    async def test_invalid_query_raises(self, ws: WebSocketConnection) -> None:
        """Invalid SurrealQL via WebSocket."""
        # WebSocket may raise QueryError directly
        try:
            result = await ws.query("INVALID QUERY SYNTAX !!!")
            # If it returns a result, check it's an error
            if result.first_result:
                assert result.first_result.is_error
        except QueryError:
            pass  # Expected

    @pytest.mark.integration
    async def test_select_nonexistent_record(self, ws: WebSocketConnection) -> None:
        """Selecting a nonexistent record via WebSocket."""
        resp = await ws.select("nonexistent_table_12345:nonexistent_id")
        assert resp.is_empty
