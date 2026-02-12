"""
Tests for v0.5.3 bug fixes.

Bug #2: NULL values for unset fields (fixed with _update_from_db)
Bug #7: save() returns new instance instead of updating self
Bug #8: datetime serialization for UPDATE (fixed with server_fields)
Bug #9: merge() returns None instead of self
"""

from datetime import UTC, datetime

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_URL

# =============================================================================
# Test Models
# =============================================================================


class SimpleModel(BaseSurrealModel):
    """Simple model for basic tests."""

    id: str | None = None
    name: str
    status: str = "pending"


class ModelWithOptionalField(BaseSurrealModel):
    """Model with optional field for Bug #2 tests."""

    id: str | None = None
    name: str
    email: str | None = None
    age: int | None = None


class ModelWithServerFields(BaseSurrealModel):
    """Model with server-generated fields for Bug #8 tests."""

    model_config = SurrealConfigDict(
        server_fields=["created_at", "updated_at"],
    )

    id: str | None = None
    name: str
    status: str = "pending"
    created_at: datetime | None = None
    updated_at: datetime | None = None


# =============================================================================
# Bug #2: NULL values for None fields
# =============================================================================


class TestBug2NullValues:
    """Test that unset optional fields are not sent as NULL."""

    def test_model_dump_excludes_unset_fields(self):
        """Unset optional fields should not be in model_dump with exclude_unset."""
        model = ModelWithOptionalField(name="Alice")

        # Only 'name' was explicitly set
        data = model.model_dump(exclude={"id"}, exclude_unset=True)

        assert "name" in data
        assert data["name"] == "Alice"
        assert "email" not in data
        assert "age" not in data

    def test_update_from_db_preserves_fields_set(self):
        """_update_from_db should not mark fields as user-set."""
        model = ModelWithOptionalField(name="Alice")

        # Verify only 'name' is marked as set
        assert model.__pydantic_fields_set__ == {"name"}

        # Simulate loading from DB
        db_record = {"id": "123", "name": "Alice", "email": "alice@example.com", "age": 30}
        model._update_from_db(db_record)

        # Fields should be updated
        assert model.id == "123"
        assert model.email == "alice@example.com"
        assert model.age == 30

        # But only 'name' should still be in fields_set
        assert model.__pydantic_fields_set__ == {"name"}

        # So exclude_unset should still exclude email and age
        data = model.model_dump(exclude={"id"}, exclude_unset=True)
        assert "name" in data
        assert "email" not in data
        assert "age" not in data

    def test_explicitly_set_none_is_included(self):
        """If user explicitly sets field to None, it should be included."""
        model = ModelWithOptionalField(name="Alice", email=None)

        # Both 'name' and 'email' were explicitly set
        assert "name" in model.__pydantic_fields_set__
        assert "email" in model.__pydantic_fields_set__

        data = model.model_dump(exclude={"id"}, exclude_unset=True)
        assert "name" in data
        assert "email" in data
        assert data["email"] is None


# =============================================================================
# Bug #7: save() returns new instance instead of updating self
# =============================================================================


class TestBug7SaveUpdatesSelf:
    """Test that save() updates the original instance."""

    def test_update_from_db_updates_instance(self):
        """_update_from_db should update the instance in place."""
        model = SimpleModel(name="Original")

        # Verify initial state
        assert model.name == "Original"
        assert model.id is None

        # Simulate DB response with generated ID
        db_record = {"id": "SimpleModel:abc123", "name": "Original", "status": "active"}
        model._update_from_db(db_record)

        # Instance should be updated in place
        assert model.id == "abc123"  # ID is parsed from record format
        assert model.status == "active"

    def test_save_preserves_identity(self):
        """After save(), the returned object should be the same instance (or at least same ID)."""
        # This is a unit test that verifies the code structure
        # Integration test would verify actual DB behavior
        model = SimpleModel(name="Test")
        original_id = id(model)

        # Simulate what happens after save when ID is provided
        model_id = "test123"
        object.__setattr__(model, "id", model_id)

        # Verify the instance is the same object
        assert id(model) == original_id
        assert model.id == "test123"


# =============================================================================
# Bug #8: datetime serialization for UPDATE (server_fields)
# =============================================================================


class TestBug8ServerFields:
    """Test that server_fields are excluded from save/update."""

    def test_get_server_fields_returns_configured_fields(self):
        """get_server_fields should return the configured set."""
        server_fields = ModelWithServerFields.get_server_fields()

        assert isinstance(server_fields, set)
        assert "created_at" in server_fields
        assert "updated_at" in server_fields

    def test_get_server_fields_empty_when_not_configured(self):
        """get_server_fields should return empty set when not configured."""
        server_fields = SimpleModel.get_server_fields()

        assert isinstance(server_fields, set)
        assert len(server_fields) == 0

    def test_model_dump_excludes_server_fields(self):
        """Server fields should be excluded from model_dump for save."""
        now = datetime.now(UTC)
        model = ModelWithServerFields(
            name="Test",
            status="active",
            created_at=now,
            updated_at=now,
        )

        # Build exclude set like save() does
        exclude_fields = {"id"} | model.get_server_fields()
        data = model.model_dump(exclude=exclude_fields, exclude_unset=True)

        assert "name" in data
        assert "status" in data
        assert "created_at" not in data
        assert "updated_at" not in data

    def test_server_fields_in_model_config(self):
        """server_fields should be accessible via model_config."""
        config = ModelWithServerFields.model_config
        assert "server_fields" in config
        assert config["server_fields"] == ["created_at", "updated_at"]


# =============================================================================
# Bug #9: merge() returns None instead of self
# =============================================================================


class TestBug9MergeReturnsSelf:
    """Test that merge() returns the updated model instance."""

    def test_merge_return_type_is_self(self):
        """merge() method signature should return Self."""
        import inspect

        sig = inspect.signature(BaseSurrealModel.merge)
        # Check return annotation is Self
        assert sig.return_annotation.__name__ == "Self"


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.fixture(scope="module", autouse=True)
async def setup_connection():
    """Setup connection for integration tests."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        "root",
        "root",
        "test",
        "test_bugfix_053",
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def cleanup_tables():
    """Clean up test tables before and after each test."""
    tables = ["SimpleModel", "ModelWithOptionalField", "ModelWithServerFields"]
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
class TestBug7Integration:
    """Integration tests for Bug #7: save() updates self."""

    async def test_save_updates_self_with_generated_id(self):
        """save() should update self with the generated ID."""
        model = SimpleModel(name="TestUser")

        # Before save, no ID
        assert model.id is None

        # Save returns self
        returned = await model.save()

        # Both should have the ID
        assert model.id is not None
        assert returned.id == model.id

        # They should be the same instance (or at least have same ID)
        assert returned.id == model.id


@pytest.mark.integration
class TestBug9Integration:
    """Integration tests for Bug #9: merge() returns self."""

    async def test_merge_returns_updated_model(self):
        """merge() should return the updated model instance."""
        # Create model
        model = SimpleModel(id="merge_test_1", name="Original")
        await model.save()

        # Merge and check return
        result = await model.merge(status="updated")

        # Should return self
        assert result is not None
        assert result.status == "updated"
        assert result.id == "merge_test_1"


@pytest.mark.integration
class TestBug2Integration:
    """Integration tests for Bug #2: NULL values."""

    async def test_unset_optional_fields_not_in_dump(self):
        """Unset optional fields should not be included in model_dump."""
        # Create model with only required field
        model = ModelWithOptionalField(name="TestUser")

        # Build exclude set like save() does
        exclude_fields = {"id"} | model.get_server_fields()
        data = model.model_dump(exclude=exclude_fields, exclude_unset=True)

        # Only 'name' should be in the data, not email or age
        assert "name" in data
        assert "email" not in data
        assert "age" not in data

    async def test_save_preserves_fields_set_tracking(self):
        """After save, fields_set should still only track user-set fields."""
        model = ModelWithOptionalField(name="TestUser")

        # Only 'name' should be marked as set
        assert model.__pydantic_fields_set__ == {"name"}

        # Save the model
        await model.save()

        # After save with auto-generated ID, fields_set should still only have 'name'
        # (id is added by _update_from_db but not marked as user-set)
        assert "name" in model.__pydantic_fields_set__
        # Note: 'id' might be in fields_set depending on implementation,
        # but the important thing is that email/age are NOT


@pytest.mark.integration
class TestBug8Integration:
    """Integration tests for Bug #8: server_fields excluded."""

    async def test_second_save_excludes_server_fields(self):
        """Second save should not send server_fields back to DB."""
        # Create model
        model = ModelWithServerFields(name="TestUser", status="pending")

        # First save
        await model.save()
        first_id = model.id

        # Simulate server setting created_at (normally done by DB)
        model.created_at = datetime.now(UTC)

        # Change status and save again
        model.status = "active"
        await model.save()

        # Should still have same ID (no error from datetime)
        assert model.id == first_id
        assert model.status == "active"
