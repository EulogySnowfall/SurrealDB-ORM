"""
Tests for v0.5.3.1 bug fixes.

Bug #10: UPDATE sends NONE for unmodified fields (fixed with merge() instead of upsert())
Bug #11: datetime returned as string instead of datetime object
"""

from datetime import datetime, timezone

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    _parse_datetime,
    _is_datetime_field,
)

from tests.conftest import SURREALDB_URL


# =============================================================================
# Test Models
# =============================================================================


class ModelWithDatetime(BaseSurrealModel):
    """Model with datetime field for Bug #10 and #11 tests."""

    model_config = SurrealConfigDict(
        server_fields=["created_at"],
    )

    id: str | None = None
    name: str
    status: str = "new"
    created_at: datetime | None = None


# =============================================================================
# Bug #10: UPDATE sends NONE for unmodified fields
# =============================================================================


class TestBug10MergeInsteadOfUpsert:
    """Test that save() uses merge() for partial updates."""

    def test_model_dump_excludes_unset_datetime(self):
        """Unset datetime fields should not be in model_dump with exclude_unset."""
        model = ModelWithDatetime(name="Test")

        # Only 'name' was explicitly set
        data = model.model_dump(exclude={"id"}, exclude_unset=True)

        assert "name" in data
        assert data["name"] == "Test"
        # status has a default value but wasn't explicitly set by user
        # created_at should NOT be in the dump
        assert "created_at" not in data

    def test_update_from_db_preserves_fields_set_for_datetime(self):
        """_update_from_db should not mark datetime fields as user-set."""
        model = ModelWithDatetime(name="Test")

        # Verify only 'name' is marked as set
        assert "name" in model.__pydantic_fields_set__
        assert "created_at" not in model.__pydantic_fields_set__

        # Simulate loading from DB
        db_record = {
            "id": "123",
            "name": "Test",
            "status": "new",
            "created_at": "2026-02-02T13:21:23.641315924Z",
        }
        model._update_from_db(db_record)

        # Fields should be updated
        assert model.id == "123"
        assert model.created_at is not None

        # But only 'name' should still be in fields_set
        assert "name" in model.__pydantic_fields_set__
        assert "created_at" not in model.__pydantic_fields_set__

        # So exclude_unset should still exclude created_at
        exclude_fields = {"id"} | model.get_server_fields()
        data = model.model_dump(exclude=exclude_fields, exclude_unset=True)
        assert "name" in data
        assert "created_at" not in data


# =============================================================================
# Bug #11: datetime returned as string
# =============================================================================


class TestBug11DatetimeParsing:
    """Test that datetime fields are properly parsed from DB response."""

    def test_parse_datetime_from_iso_string(self):
        """_parse_datetime should convert ISO string to datetime."""
        iso_string = "2026-02-02T13:21:23.641315924Z"
        result = _parse_datetime(iso_string)

        assert isinstance(result, datetime)
        assert result.year == 2026
        assert result.month == 2
        assert result.day == 2

    def test_parse_datetime_preserves_datetime(self):
        """_parse_datetime should preserve existing datetime objects."""
        dt = datetime(2026, 2, 2, 13, 21, 23, tzinfo=timezone.utc)
        result = _parse_datetime(dt)

        assert result is dt

    def test_parse_datetime_handles_none(self):
        """_parse_datetime should return None for None input."""
        result = _parse_datetime(None)
        assert result is None

    def test_is_datetime_field_simple(self):
        """_is_datetime_field should detect simple datetime type."""
        assert _is_datetime_field(datetime) is True
        assert _is_datetime_field(str) is False
        assert _is_datetime_field(int) is False

    def test_is_datetime_field_optional(self):
        """_is_datetime_field should detect Optional[datetime]."""
        from typing import Optional

        assert _is_datetime_field(datetime | None) is True
        assert _is_datetime_field(Optional[datetime]) is True
        assert _is_datetime_field(str | None) is False

    def test_update_from_db_parses_datetime(self):
        """_update_from_db should parse datetime strings to datetime objects."""
        model = ModelWithDatetime(name="Test")

        # Simulate loading from DB with datetime as ISO string
        db_record = {
            "id": "123",
            "name": "Test",
            "status": "new",
            "created_at": "2026-02-02T13:21:23.641315924Z",
        }
        model._update_from_db(db_record)

        # created_at should be a datetime, not a string
        assert isinstance(model.created_at, datetime)
        assert model.created_at.year == 2026


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
        "test_bugfix_0531",
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def cleanup_tables():
    """Clean up test tables before and after each test."""
    tables = ["ModelWithDatetime"]
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
class TestBug10Integration:
    """Integration tests for Bug #10: UPDATE preserves unmodified fields."""

    async def test_save_then_update_preserves_datetime(self):
        """Second save should not overwrite created_at with NONE."""
        # Create model
        model = ModelWithDatetime(name="Test", status="new")

        # First save - let DB set created_at
        await model.save()
        model_id = model.id
        assert model_id is not None

        # Fetch to get created_at value (simulating what happens in real usage)
        await model.refresh()
        original_created_at = model.created_at

        # Now update only status
        model.status = "playing"
        await model.save()

        # Verify it worked - no error and status updated
        assert model.id == model_id
        assert model.status == "playing"

        # Fetch fresh from DB to verify created_at wasn't overwritten
        await model.refresh()
        # If created_at was set by DB and we saved it, it should be preserved
        if original_created_at is not None:
            assert model.created_at == original_created_at


@pytest.mark.integration
class TestBug11Integration:
    """Integration tests for Bug #11: datetime parsed correctly."""

    async def test_datetime_type_after_save_and_refresh(self):
        """Datetime fields should be datetime objects after refresh."""
        model = ModelWithDatetime(name="Test", status="new")
        await model.save()

        # Refresh to get server-set values
        await model.refresh()

        # If created_at was set, it should be a datetime
        if model.created_at is not None:
            assert isinstance(model.created_at, datetime)
