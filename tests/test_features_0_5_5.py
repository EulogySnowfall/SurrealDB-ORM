"""
Tests for v0.5.5 features.

Feature #1: CBOR protocol support for WebSocket connections
Feature #2: Sync version of unset_connection()
Feature #3: Field alias support (Python name vs DB column name)
"""

from typing import AsyncGenerator

import pytest
from pydantic import Field

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel


# Test URLs - use same ports as other integration tests
SURREALDB_URL = "http://localhost:8001"


# =============================================================================
# Test Models
# =============================================================================


class UserWithAlias(BaseSurrealModel):
    """User model with field alias for password."""

    id: str | None = None
    email: str
    # Python field 'password' maps to 'password_hash' in DB
    password: str = Field(alias="password_hash")


class ProfileWithAliases(BaseSurrealModel):
    """Profile model with multiple field aliases."""

    id: str | None = None
    # Python 'full_name' maps to 'name' in DB
    full_name: str = Field(alias="name")
    # Python 'user_age' maps to 'age' in DB
    user_age: int = Field(alias="age", default=0)


# =============================================================================
# Feature #1: CBOR Protocol (Unit Tests)
# =============================================================================


class TestCBORProtocol:
    """Test CBOR protocol support."""

    def test_cbor_available_check(self) -> None:
        """Test that CBOR availability can be checked."""
        from src.surreal_sdk.protocol.cbor import is_available

        # Should return a boolean
        result = is_available()
        assert isinstance(result, bool)

    def test_websocket_protocol_parameter(self) -> None:
        """Test that WebSocketConnection accepts protocol parameter."""
        from src.surreal_sdk.connection.websocket import WebSocketConnection

        # Default protocol is cbor (required dependency)
        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        assert conn.protocol == "cbor"

        # Can explicitly set json for debugging/compatibility
        conn_json = WebSocketConnection("ws://localhost:8000", "ns", "db", protocol="json")
        assert conn_json.protocol == "json"

        # Can explicitly set cbor
        conn_cbor = WebSocketConnection("ws://localhost:8000", "ns", "db", protocol="cbor")
        assert conn_cbor.protocol == "cbor"

    def test_websocket_invalid_protocol_raises(self) -> None:
        """Test that invalid protocol raises ValueError."""
        from src.surreal_sdk.connection.websocket import WebSocketConnection

        with pytest.raises(ValueError, match="Invalid protocol"):
            WebSocketConnection("ws://localhost:8000", "ns", "db", protocol="invalid")


# =============================================================================
# Feature #2: Sync unset_connection()
# =============================================================================


class TestSyncUnsetConnection:
    """Test synchronous unset_connection."""

    def test_unset_connection_sync_clears_settings(self) -> None:
        """unset_connection_sync() should clear all connection settings."""
        # Setup with a different database to avoid affecting other tests
        SurrealDBConnectionManager.set_connection(
            SURREALDB_URL,
            "root",
            "root",
            "test",
            "test_sync_unset",
        )
        assert SurrealDBConnectionManager.is_connection_set() is True

        # Test sync unset
        SurrealDBConnectionManager.unset_connection_sync()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert SurrealDBConnectionManager.is_connected() is False

        # Restore connection for subsequent tests in this module
        SurrealDBConnectionManager.set_connection(
            SURREALDB_URL,
            "root",
            "root",
            "test",
            "test_features_055",
        )


# =============================================================================
# Feature #3: Field Alias Support (Unit Tests)
# =============================================================================


class TestFieldAliasSupport:
    """Test that field aliases work correctly."""

    def test_model_dump_uses_alias(self) -> None:
        """model_dump(by_alias=True) should use alias names."""
        user = UserWithAlias(email="test@example.com", password="secret123")

        # Without by_alias, uses field names
        data_no_alias = user.model_dump()
        assert "password" in data_no_alias
        assert "password_hash" not in data_no_alias

        # With by_alias, uses alias names (as ORM does when saving)
        data_with_alias = user.model_dump(by_alias=True)
        assert "password_hash" in data_with_alias
        assert "password" not in data_with_alias
        assert data_with_alias["password_hash"] == "secret123"

    def test_model_validates_both_names(self) -> None:
        """Model should accept both field name and alias when loading."""
        # Load using alias (as DB returns)
        user1 = UserWithAlias.model_validate(
            {
                "email": "test@example.com",
                "password_hash": "secret123",
            }
        )
        assert user1.password == "secret123"

        # Load using field name (also works with populate_by_name=True)
        user2 = UserWithAlias.model_validate(
            {
                "email": "test@example.com",
                "password": "secret456",
            }
        )
        assert user2.password == "secret456"

    def test_multiple_aliases(self) -> None:
        """Test model with multiple field aliases."""
        profile = ProfileWithAliases(full_name="John Doe", user_age=30)

        # Dump with aliases (for DB)
        data = profile.model_dump(by_alias=True)
        assert data["name"] == "John Doe"
        assert data["age"] == 30
        assert "full_name" not in data
        assert "user_age" not in data

    def test_alias_with_exclude_fields(self) -> None:
        """Test that alias works correctly with exclude fields."""
        user = UserWithAlias(email="test@example.com", password="secret123")

        # Simulate what save() does
        exclude_fields = {"id"}
        data = user.model_dump(exclude=exclude_fields, exclude_unset=True, by_alias=True)

        # Should have alias names, not field names
        assert "password_hash" in data
        assert "password" not in data
        assert "email" in data
        assert "id" not in data


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, None]:
    """Setup connection for integration tests."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        "root",
        "root",
        "test",
        "test_features_055",
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def cleanup_tables() -> AsyncGenerator[None, None]:
    """Clean up test tables before and after each test."""
    tables = ["UserWithAlias", "ProfileWithAliases"]
    try:
        client = await SurrealDBConnectionManager.get_client()
        for table in tables:
            try:
                await client.query(f"DELETE {table};")
            except Exception:
                pass
    except Exception:
        pass
    yield
    try:
        client = await SurrealDBConnectionManager.get_client()
        for table in tables:
            try:
                await client.query(f"DELETE {table};")
            except Exception:
                pass
    except Exception:
        pass


@pytest.mark.integration
class TestFieldAliasIntegration:
    """Integration tests for field alias support."""

    async def test_save_with_alias(self) -> None:
        """save() should use alias names when writing to DB."""
        user = UserWithAlias(id="alias_test_1", email="test@example.com", password="secret123")
        await user.save()

        # Query the DB directly to verify the column name
        client = await SurrealDBConnectionManager.get_client()
        result = await client.query("SELECT * FROM UserWithAlias:alias_test_1")

        # The DB should have 'password_hash', not 'password'
        records = result.all_records
        assert len(records) == 1
        assert "password_hash" in records[0]
        assert records[0]["password_hash"] == "secret123"
        # Field name 'password' should NOT be in DB
        assert "password" not in records[0]

    async def test_load_with_alias(self) -> None:
        """Loading from DB should map alias back to field name."""
        # Insert directly using alias
        client = await SurrealDBConnectionManager.get_client()
        await client.query("CREATE UserWithAlias:alias_test_2 SET email = 'load@example.com', password_hash = 'loaded123'")

        # Fetch using ORM
        user = await UserWithAlias.objects().get("alias_test_2")

        # Should be accessible via Python field name
        assert user.email == "load@example.com"
        assert user.password == "loaded123"

    async def test_save_and_reload_with_alias(self) -> None:
        """Full round-trip: save with alias, reload, modify, save again."""
        # Create and save
        user = UserWithAlias(id="alias_test_3", email="roundtrip@example.com", password="initial")
        await user.save()

        # Reload
        loaded = await UserWithAlias.objects().get("alias_test_3")
        assert loaded.password == "initial"

        # Modify and save again
        loaded.password = "updated"
        await loaded.save()

        # Reload and verify
        reloaded = await UserWithAlias.objects().get("alias_test_3")
        assert reloaded.password == "updated"

        # Verify DB has alias name
        client = await SurrealDBConnectionManager.get_client()
        result = await client.query("SELECT * FROM UserWithAlias:alias_test_3")
        assert result.all_records[0]["password_hash"] == "updated"

    async def test_multiple_aliases_save_and_load(self) -> None:
        """Test model with multiple aliases saves and loads correctly."""
        profile = ProfileWithAliases(id="multi_alias_1", full_name="Jane Doe", user_age=25)
        await profile.save()

        # Verify DB has alias names
        client = await SurrealDBConnectionManager.get_client()
        result = await client.query("SELECT * FROM ProfileWithAliases:multi_alias_1")
        assert result.all_records[0]["name"] == "Jane Doe"
        assert result.all_records[0]["age"] == 25

        # Reload and verify
        loaded = await ProfileWithAliases.objects().get("multi_alias_1")
        assert loaded.full_name == "Jane Doe"
        assert loaded.user_age == 25
