"""Tests for ORM v0.3.1 features: bulk operations."""

from pydantic import Field

from src.surreal_orm.model_base import BaseSurrealModel
from src.surreal_orm.query_set import QuerySet


class Item(BaseSurrealModel):
    """Test model for bulk operation tests."""

    id: str | None = None
    name: str = Field(...)
    category: str = Field(...)
    price: float = Field(..., ge=0)
    active: bool = Field(default=True)


# ==================== bulk_create() Unit Tests ====================


def test_queryset_has_bulk_create_method() -> None:
    """Test that QuerySet has bulk_create method."""
    qs = Item.objects()
    assert hasattr(qs, "bulk_create")
    assert callable(qs.bulk_create)


def test_bulk_create_signature() -> None:
    """Test bulk_create method signature."""
    import inspect

    sig = inspect.signature(QuerySet.bulk_create)
    params = list(sig.parameters.keys())
    assert "instances" in params
    assert "atomic" in params
    assert "batch_size" in params

    # Check defaults
    assert sig.parameters["atomic"].default is False
    assert sig.parameters["batch_size"].default is None


# ==================== bulk_update() Unit Tests ====================


def test_queryset_has_bulk_update_method() -> None:
    """Test that QuerySet has bulk_update method."""
    qs = Item.objects()
    assert hasattr(qs, "bulk_update")
    assert callable(qs.bulk_update)


def test_bulk_update_signature() -> None:
    """Test bulk_update method signature."""
    import inspect

    sig = inspect.signature(QuerySet.bulk_update)
    params = list(sig.parameters.keys())
    assert "data" in params
    assert "atomic" in params

    # Check defaults
    assert sig.parameters["atomic"].default is False


# ==================== bulk_delete() Unit Tests ====================


def test_queryset_has_bulk_delete_method() -> None:
    """Test that QuerySet has bulk_delete method."""
    qs = Item.objects()
    assert hasattr(qs, "bulk_delete")
    assert callable(qs.bulk_delete)


def test_bulk_delete_signature() -> None:
    """Test bulk_delete method signature."""
    import inspect

    sig = inspect.signature(QuerySet.bulk_delete)
    params = list(sig.parameters.keys())
    assert "atomic" in params

    # Check defaults
    assert sig.parameters["atomic"].default is False


# ==================== QuerySet Chaining Tests ====================


def test_bulk_operations_work_with_filters() -> None:
    """Test that bulk operations respect filters."""
    qs = Item.objects().filter(category="Electronics", active=True)
    assert qs._filters == [("category", "exact", "Electronics"), ("active", "exact", True)]
    # The bulk methods should use these filters
    assert hasattr(qs, "bulk_update")
    assert hasattr(qs, "bulk_delete")
