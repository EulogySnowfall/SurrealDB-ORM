"""Integration tests for ORM v0.3.1 features: bulk operations."""

import pytest
from pydantic import Field
from typing import AsyncGenerator, Any
from src.surreal_orm.model_base import BaseSurrealModel
from src.surreal_orm import SurrealDBConnectionManager
from tests.conftest import SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE


SURREALDB_DATABASE = "test_bulk"


class Item(BaseSurrealModel):
    """Test model for bulk operation tests."""

    id: str | None = None
    name: str = Field(...)
    category: str = Field(...)
    price: float = Field(..., ge=0)
    active: bool = Field(default=True)


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[Any, Any]:
    """Setup connection for the test module."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def cleanup_items() -> AsyncGenerator[Any, Any]:
    """Clean up items table before and after each test."""
    try:
        await Item.objects().delete_table()
    except Exception:
        pass
    yield
    try:
        await Item.objects().delete_table()
    except Exception:
        pass


# ==================== bulk_create() Integration Tests ====================


@pytest.mark.integration
async def test_bulk_create_simple() -> None:
    """Test simple bulk create."""
    items = [Item(name=f"Item{i}", category="Test", price=10.0 * i) for i in range(1, 6)]

    created = await Item.objects().bulk_create(items)

    assert len(created) == 5
    count = await Item.objects().count()
    assert count == 5


@pytest.mark.integration
async def test_bulk_create_empty_list() -> None:
    """Test bulk create with empty list."""
    created = await Item.objects().bulk_create([])

    assert len(created) == 0
    count = await Item.objects().count()
    assert count == 0


@pytest.mark.integration
async def test_bulk_create_atomic() -> None:
    """Test atomic bulk create."""
    items = [Item(name=f"AtomicItem{i}", category="Atomic", price=5.0) for i in range(1, 4)]

    created = await Item.objects().bulk_create(items, atomic=True)

    assert len(created) == 3
    count = await Item.objects().filter(category="Atomic").count()
    assert count == 3


@pytest.mark.integration
async def test_bulk_create_with_batch_size() -> None:
    """Test bulk create with batch size."""
    items = [Item(name=f"BatchItem{i}", category="Batch", price=1.0) for i in range(1, 11)]

    created = await Item.objects().bulk_create(items, batch_size=3)

    assert len(created) == 10
    count = await Item.objects().filter(category="Batch").count()
    assert count == 10


# ==================== bulk_update() Integration Tests ====================


@pytest.fixture
async def sample_items() -> list[Item]:
    """Create sample items for update/delete tests."""
    items = [
        Item(name="Laptop", category="Electronics", price=999.99, active=True),
        Item(name="Phone", category="Electronics", price=499.99, active=True),
        Item(name="Tablet", category="Electronics", price=399.99, active=False),
        Item(name="Chair", category="Furniture", price=149.99, active=True),
        Item(name="Desk", category="Furniture", price=299.99, active=False),
    ]

    for item in items:
        await item.save()

    return items


@pytest.mark.integration
async def test_bulk_update_all(sample_items: list[Item]) -> None:
    """Test bulk update all records."""
    updated = await Item.objects().bulk_update({"active": False})

    assert updated == 5
    inactive_count = await Item.objects().filter(active=False).count()
    assert inactive_count == 5


@pytest.mark.integration
async def test_bulk_update_with_filter(sample_items: list[Item]) -> None:
    """Test bulk update with filter."""
    updated = await Item.objects().filter(category="Electronics").bulk_update({"price": 100.0})

    assert updated == 3  # 3 electronics items

    # Verify prices updated
    cheap_count = await Item.objects().filter(price=100.0).count()
    assert cheap_count == 3


@pytest.mark.integration
async def test_bulk_update_atomic(sample_items: list[Item]) -> None:
    """Test atomic bulk update."""
    updated = await Item.objects().filter(category="Furniture").bulk_update({"active": True, "price": 50.0}, atomic=True)

    assert updated == 2  # 2 furniture items


@pytest.mark.integration
async def test_bulk_update_no_match(sample_items: list[Item]) -> None:
    """Test bulk update with no matching records."""
    updated = await Item.objects().filter(category="NonExistent").bulk_update({"active": False})

    assert updated == 0


# ==================== bulk_delete() Integration Tests ====================


@pytest.mark.integration
async def test_bulk_delete_with_filter(sample_items: list[Item]) -> None:
    """Test bulk delete with filter."""
    deleted = await Item.objects().filter(active=False).bulk_delete()

    assert deleted == 2  # Tablet and Desk

    remaining = await Item.objects().count()
    assert remaining == 3


@pytest.mark.integration
async def test_bulk_delete_all(sample_items: list[Item]) -> None:
    """Test bulk delete all records."""
    deleted = await Item.objects().bulk_delete()

    assert deleted == 5

    remaining = await Item.objects().count()
    assert remaining == 0


@pytest.mark.integration
async def test_bulk_delete_atomic(sample_items: list[Item]) -> None:
    """Test atomic bulk delete."""
    deleted = await Item.objects().filter(category="Furniture").bulk_delete(atomic=True)

    assert deleted == 2

    remaining = await Item.objects().count()
    assert remaining == 3


@pytest.mark.integration
async def test_bulk_delete_no_match(sample_items: list[Item]) -> None:
    """Test bulk delete with no matching records."""
    deleted = await Item.objects().filter(category="NonExistent").bulk_delete()

    assert deleted == 0

    remaining = await Item.objects().count()
    assert remaining == 5


# ==================== Combined Operations Tests ====================


@pytest.mark.integration
async def test_bulk_create_then_update_then_delete() -> None:
    """Test a sequence of bulk operations."""
    # Create
    items = [Item(name=f"SeqItem{i}", category="Sequence", price=10.0) for i in range(1, 6)]
    await Item.objects().bulk_create(items)

    # Update
    updated = await Item.objects().filter(category="Sequence").bulk_update({"price": 20.0})
    assert updated == 5

    # Verify update
    avg_price = await Item.objects().filter(category="Sequence").avg("price")
    assert avg_price == 20.0

    # Delete
    deleted = await Item.objects().filter(category="Sequence").bulk_delete()
    assert deleted == 5

    # Verify delete
    count = await Item.objects().filter(category="Sequence").count()
    assert count == 0
