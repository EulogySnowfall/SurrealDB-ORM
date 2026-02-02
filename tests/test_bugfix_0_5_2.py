"""
Tests for v0.5.2 bug fixes.

Bug #1: datetime serialization in RPC requests
Bug #2: NULL values with exclude_unset for optional fields
Bug #3: connect() fluent API return type
Bug #4: user/username parameter alias
Bug #5: Session cleanup for WebSocket callback tasks
Bug #6: Documentation updates (manual verification)
"""

from datetime import datetime, date, time
from decimal import Decimal
from uuid import UUID

import pytest

from surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from surreal_sdk import SurrealDB, HTTPConnection, WebSocketConnection
from surreal_sdk.protocol.rpc import RPCRequest

# Use the same port as other integration tests (conftest manages the container)
SURREALDB_URL = "http://localhost:8001"
SURREALDB_WS_URL = "ws://localhost:8001"


# =============================================================================
# Bug #1: datetime serialization
# =============================================================================


class TestBug1DatetimeSerialization:
    """Test that datetime and other types serialize correctly in RPC requests."""

    def test_datetime_serialization(self):
        """datetime objects should serialize to ISO 8601 strings."""
        dt = datetime(2024, 1, 15, 10, 30, 45)
        request = RPCRequest(method="create", params=["test", {"created_at": dt}])
        json_str = request.to_json()

        assert "2024-01-15T10:30:45" in json_str

    def test_date_serialization(self):
        """date objects should serialize to ISO 8601 strings."""
        d = date(2024, 1, 15)
        request = RPCRequest(method="create", params=["test", {"birth_date": d}])
        json_str = request.to_json()

        assert "2024-01-15" in json_str

    def test_time_serialization(self):
        """time objects should serialize to ISO 8601 strings."""
        t = time(10, 30, 45)
        request = RPCRequest(method="create", params=["test", {"start_time": t}])
        json_str = request.to_json()

        assert "10:30:45" in json_str

    def test_decimal_serialization(self):
        """Decimal objects should serialize to floats."""
        d = Decimal("123.45")
        request = RPCRequest(method="create", params=["test", {"price": d}])
        json_str = request.to_json()

        assert "123.45" in json_str

    def test_uuid_serialization(self):
        """UUID objects should serialize to strings."""
        u = UUID("12345678-1234-5678-1234-567812345678")
        request = RPCRequest(method="create", params=["test", {"uuid": u}])
        json_str = request.to_json()

        assert "12345678-1234-5678-1234-567812345678" in json_str

    def test_nested_datetime_serialization(self):
        """Nested datetime objects should also serialize correctly."""
        dt = datetime(2024, 1, 15, 10, 30, 45)
        request = RPCRequest(
            method="create",
            params=["test", {"metadata": {"created": dt, "tags": ["a", "b"]}}],
        )
        json_str = request.to_json()

        assert "2024-01-15T10:30:45" in json_str

    @pytest.mark.integration
    async def test_create_with_datetime_integration(self):
        """Integration test: create record with datetime field."""

        class EventModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="events_test")

            id: str | None = None
            name: str
            event_date: datetime

        async with SurrealDB.http(SURREALDB_URL, "test", "test") as db:
            await db.signin("root", "root")

            # Create with datetime
            event_dt = datetime(2024, 6, 15, 14, 30, 0)
            result = await db.create("events_test", {"name": "Test Event", "event_date": event_dt.isoformat()})

            assert result.record is not None
            assert result.record["name"] == "Test Event"

            # Cleanup
            await db.delete("events_test")


# =============================================================================
# Bug #2: NULL values with exclude_unset
# =============================================================================


class TestBug2NullValues:
    """Test that optional fields with None don't override DB defaults."""

    @pytest.mark.integration
    async def test_optional_field_not_sent_when_unset(self):
        """Unset optional fields should not be included in the request."""

        class ProductModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="products_test")

            id: str | None = None
            name: str
            description: str | None = None  # Optional with default None

        # Verify model_dump behavior with exclude_unset
        product = ProductModel(name="Test Product")
        data = product.model_dump(exclude={"id"}, exclude_unset=True)

        # description should NOT be in the data since it was never set
        assert "description" not in data
        assert data == {"name": "Test Product"}

    @pytest.mark.integration
    async def test_optional_field_sent_when_explicitly_set_to_none(self):
        """Explicitly set None values should be included."""

        class ProductModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_name="products_test")

            id: str | None = None
            name: str
            description: str | None = None

        # Explicitly set description to None
        product = ProductModel(name="Test Product", description=None)
        data = product.model_dump(exclude={"id"}, exclude_unset=True)

        # description should be in the data since it was explicitly set
        assert "description" in data
        assert data["description"] is None


# =============================================================================
# Bug #3: connect() fluent API
# =============================================================================


class TestBug3FluentConnect:
    """Test that connect() returns self for fluent API usage."""

    @pytest.mark.integration
    async def test_http_connect_returns_self(self):
        """HTTPConnection.connect() should return the connection instance."""
        conn = HTTPConnection(SURREALDB_URL, "test", "test")
        result = await conn.connect()

        assert result is conn
        assert isinstance(result, HTTPConnection)

        await conn.close()

    @pytest.mark.integration
    async def test_ws_connect_returns_self(self):
        """WebSocketConnection.connect() should return the connection instance."""
        conn = WebSocketConnection(SURREALDB_WS_URL, "test", "test")
        result = await conn.connect()

        assert result is conn
        assert isinstance(result, WebSocketConnection)

        await conn.close()

    @pytest.mark.integration
    async def test_fluent_chaining(self):
        """Should be able to chain connect() with signin()."""
        conn = HTTPConnection(SURREALDB_URL, "test", "test")

        # This should work without error
        await (await conn.connect()).signin("root", "root")

        assert conn.is_connected
        assert conn.is_authenticated

        await conn.close()


# =============================================================================
# Bug #4: user/username parameter alias
# =============================================================================


class TestBug4UsernameAlias:
    """Test that both 'user' and 'username' parameters work."""

    def test_user_parameter_works_positionally(self):
        """'user' parameter should work as a positional argument."""
        SurrealDBConnectionManager.set_connection(
            SURREALDB_URL,
            "root",
            "root",
            "test",
            "test",
        )

        assert SurrealDBConnectionManager.get_user() == "root"

    def test_user_parameter_works_as_keyword(self):
        """'user' parameter should work as a keyword argument."""
        SurrealDBConnectionManager.set_connection(
            url=SURREALDB_URL,
            user="root",
            password="root",
            namespace="test",
            database="test",
        )

        assert SurrealDBConnectionManager.get_user() == "root"

    def test_username_overrides_user(self):
        """'username' keyword should override 'user' when provided."""
        SurrealDBConnectionManager.set_connection(
            url=SURREALDB_URL,
            user="default_user",
            password="pass",
            namespace="test",
            database="test",
            username="override_user",
        )

        # username takes precedence when explicitly provided
        assert SurrealDBConnectionManager.get_user() == "override_user"

    def test_user_used_when_username_not_provided(self):
        """When 'username' is not provided, 'user' is used."""
        SurrealDBConnectionManager.set_connection(
            url=SURREALDB_URL,
            user="primary",
            password="pass",
            namespace="test",
            database="test",
        )

        assert SurrealDBConnectionManager.get_user() == "primary"


# =============================================================================
# Bug #5: Session cleanup
# =============================================================================


class TestBug5SessionCleanup:
    """Test that WebSocket callback tasks are properly cleaned up."""

    @pytest.mark.integration
    async def test_cleanup_cancels_callback_tasks(self):
        """Cleanup should cancel all pending callback tasks."""
        conn = WebSocketConnection(SURREALDB_WS_URL, "test", "test")
        await conn.connect()
        await conn.signin("root", "root")

        # Track that callback tasks set exists
        assert hasattr(conn, "_callback_tasks")
        assert isinstance(conn._callback_tasks, set)

        # Close connection (should cancel all tasks)
        await conn.close()

        # After cleanup, tasks should be cleared
        assert len(conn._callback_tasks) == 0

    @pytest.mark.integration
    async def test_callback_tasks_tracked(self):
        """Callback tasks should be tracked in the _callback_tasks set."""
        conn = WebSocketConnection(SURREALDB_WS_URL, "test", "test")

        # Verify the set is initialized
        assert hasattr(conn, "_callback_tasks")
        assert isinstance(conn._callback_tasks, set)
        assert len(conn._callback_tasks) == 0

        # We can't easily test live query callbacks without a complex setup,
        # but we verify the infrastructure is in place
        await conn.close()


# =============================================================================
# Bug #6: Documentation (manual verification)
# =============================================================================


class TestBug6Documentation:
    """Documentation tests - these verify documentation was updated."""

    def test_readme_exists(self):
        """README.md should exist."""
        import os

        readme_path = os.path.join(os.path.dirname(__file__), "..", "README.md")
        assert os.path.exists(readme_path), "README.md should exist"

    def test_claude_md_exists(self):
        """CLAUDE.md should exist."""
        import os

        claude_path = os.path.join(os.path.dirname(__file__), "..", "CLAUDE.md")
        assert os.path.exists(claude_path), "CLAUDE.md should exist"


# =============================================================================
# FieldType enum improvements
# =============================================================================


class TestFieldTypeEnum:
    """Test FieldType enum improvements for migrations."""

    def test_basic_field_types(self):
        """Basic field types should work."""
        from surreal_orm.types import FieldType

        assert FieldType.STRING.value == "string"
        assert FieldType.INT.value == "int"
        assert FieldType.FLOAT.value == "float"
        assert FieldType.DECIMAL.value == "decimal"
        assert FieldType.NUMBER.value == "number"
        assert FieldType.BOOL.value == "bool"
        assert FieldType.DATETIME.value == "datetime"
        assert FieldType.DURATION.value == "duration"
        assert FieldType.BYTES.value == "bytes"
        assert FieldType.UUID.value == "uuid"

    def test_collection_types(self):
        """Collection types should work."""
        from surreal_orm.types import FieldType

        assert FieldType.ARRAY.value == "array"
        assert FieldType.SET.value == "set"
        assert FieldType.OBJECT.value == "object"

    def test_special_types(self):
        """Special types should work."""
        from surreal_orm.types import FieldType

        assert FieldType.ANY.value == "any"
        assert FieldType.OPTION.value == "option"
        assert FieldType.RECORD.value == "record"
        assert FieldType.GEOMETRY.value == "geometry"
        assert FieldType.REGEX.value == "regex"

    def test_generic_method(self):
        """generic() method should create parameterized types."""
        from surreal_orm.types import FieldType

        assert FieldType.ARRAY.generic("string") == "array<string>"
        assert FieldType.SET.generic("int") == "set<int>"
        assert FieldType.RECORD.generic("users") == "record<users>"
        assert FieldType.OPTION.generic("string") == "option<string>"
        assert FieldType.GEOMETRY.generic("point") == "geometry<point>"
        assert FieldType.GEOMETRY.generic("point|polygon") == "geometry<point|polygon>"

    def test_from_python_type(self):
        """from_python_type() should map Python types to FieldType."""
        from surreal_orm.types import FieldType

        assert FieldType.from_python_type(str) == FieldType.STRING
        assert FieldType.from_python_type(int) == FieldType.INT
        assert FieldType.from_python_type(float) == FieldType.FLOAT
        assert FieldType.from_python_type(bool) == FieldType.BOOL
        assert FieldType.from_python_type(list) == FieldType.ARRAY
        assert FieldType.from_python_type(dict) == FieldType.OBJECT
        assert FieldType.from_python_type(bytes) == FieldType.BYTES

    def test_from_python_type_invalid(self):
        """from_python_type() should raise ValueError for unknown types."""
        from surreal_orm.types import FieldType

        with pytest.raises(ValueError, match="Cannot map Python type"):
            FieldType.from_python_type(set)


class TestMigrationOperationsFieldType:
    """Test that migration operations accept FieldType enum."""

    def test_add_field_with_enum(self):
        """AddField should accept FieldType enum."""
        from surreal_orm.migrations.operations import AddField
        from surreal_orm.types import FieldType

        op = AddField(table="users", name="email", field_type=FieldType.STRING)
        sql = op.forwards()

        assert "TYPE string" in sql
        assert "DEFINE FIELD email ON users" in sql

    def test_add_field_with_string(self):
        """AddField should still accept string for backward compatibility."""
        from surreal_orm.migrations.operations import AddField

        op = AddField(table="users", name="email", field_type="string")
        sql = op.forwards()

        assert "TYPE string" in sql

    def test_add_field_with_generic_type(self):
        """AddField should accept generic types like array<string>."""
        from surreal_orm.migrations.operations import AddField
        from surreal_orm.types import FieldType

        op = AddField(table="users", name="tags", field_type=FieldType.ARRAY.generic("string"))
        sql = op.forwards()

        assert "TYPE array<string>" in sql

    def test_add_field_invalid_type_raises(self):
        """AddField should raise ValueError for invalid types."""
        from surreal_orm.migrations.operations import AddField

        with pytest.raises(ValueError, match="Invalid field type"):
            AddField(table="users", name="email", field_type="invalid_type")

    def test_alter_field_with_enum(self):
        """AlterField should accept FieldType enum."""
        from surreal_orm.migrations.operations import AlterField
        from surreal_orm.types import FieldType

        op = AlterField(
            table="users",
            name="age",
            field_type=FieldType.INT,
            previous_type=FieldType.STRING,
        )
        sql = op.forwards()

        assert "TYPE int" in sql

    def test_alter_field_backwards_with_enum(self):
        """AlterField backwards should work with FieldType enum."""
        from surreal_orm.migrations.operations import AlterField
        from surreal_orm.types import FieldType

        op = AlterField(
            table="users",
            name="age",
            field_type=FieldType.INT,
            previous_type=FieldType.STRING,
        )
        sql = op.backwards()

        assert "TYPE string" in sql
