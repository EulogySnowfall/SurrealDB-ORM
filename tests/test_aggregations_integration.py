"""Integration tests for ORM v0.3.0 features: aggregations and GROUP BY."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import Avg, Count, Max, Min, Sum, SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_aggregations"


class Product(BaseSurrealModel):
    """Test model for aggregation tests."""

    id: str | None = None
    name: str = Field(...)
    category: str = Field(...)
    price: float = Field(..., ge=0)
    stock: int = Field(..., ge=0)


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
async def cleanup_products() -> AsyncGenerator[Any, Any]:
    """Clean up products table before and after each test."""
    try:
        await Product.objects().delete_table()
    except Exception:
        pass
    yield
    try:
        await Product.objects().delete_table()
    except Exception:
        pass


@pytest.fixture
async def sample_products() -> list[Product]:
    """Create sample products for testing."""
    products = [
        Product(name="Laptop", category="Electronics", price=999.99, stock=10),
        Product(name="Phone", category="Electronics", price=499.99, stock=25),
        Product(name="Tablet", category="Electronics", price=399.99, stock=15),
        Product(name="Chair", category="Furniture", price=149.99, stock=50),
        Product(name="Desk", category="Furniture", price=299.99, stock=20),
        Product(name="Book A", category="Books", price=19.99, stock=100),
        Product(name="Book B", category="Books", price=24.99, stock=80),
    ]

    for product in products:
        await product.save()

    return products


# ==================== Simple Aggregation Tests ====================


@pytest.mark.integration
async def test_count_all(sample_products: list[Product]) -> None:
    """Test counting all records."""
    count = await Product.objects().count()
    assert count == 7


@pytest.mark.integration
async def test_count_with_filter(sample_products: list[Product]) -> None:
    """Test counting with filter."""
    count = await Product.objects().filter(category="Electronics").count()
    assert count == 3


@pytest.mark.integration
async def test_sum_field(sample_products: list[Product]) -> None:
    """Test sum of a numeric field."""
    total = await Product.objects().sum("price")
    expected = 999.99 + 499.99 + 399.99 + 149.99 + 299.99 + 19.99 + 24.99
    assert abs(total - expected) < 0.01  # Float comparison


@pytest.mark.integration
async def test_sum_with_filter(sample_products: list[Product]) -> None:
    """Test sum with filter."""
    total = await Product.objects().filter(category="Electronics").sum("price")
    expected = 999.99 + 499.99 + 399.99
    assert abs(total - expected) < 0.01


@pytest.mark.integration
async def test_avg_field(sample_products: list[Product]) -> None:
    """Test average of a numeric field."""
    avg_stock = await Product.objects().avg("stock")
    expected = (10 + 25 + 15 + 50 + 20 + 100 + 80) / 7
    assert avg_stock is not None
    assert abs(avg_stock - expected) < 0.01


@pytest.mark.integration
async def test_avg_with_filter(sample_products: list[Product]) -> None:
    """Test average with filter."""
    avg_price = await Product.objects().filter(category="Books").avg("price")
    expected = (19.99 + 24.99) / 2
    assert avg_price is not None
    assert abs(avg_price - expected) < 0.01


@pytest.mark.integration
async def test_min_field(sample_products: list[Product]) -> None:
    """Test minimum of a field."""
    min_price = await Product.objects().min("price")
    assert abs(min_price - 19.99) < 0.01


@pytest.mark.integration
async def test_min_with_filter(sample_products: list[Product]) -> None:
    """Test minimum with filter."""
    min_price = await Product.objects().filter(category="Electronics").min("price")
    assert abs(min_price - 399.99) < 0.01


@pytest.mark.integration
async def test_max_field(sample_products: list[Product]) -> None:
    """Test maximum of a field."""
    max_price = await Product.objects().max("price")
    assert abs(max_price - 999.99) < 0.01


@pytest.mark.integration
async def test_max_with_filter(sample_products: list[Product]) -> None:
    """Test maximum with filter."""
    max_stock = await Product.objects().filter(category="Books").max("stock")
    assert max_stock == 100


# ==================== GROUP BY Tests ====================


@pytest.mark.integration
async def test_group_by_single_field(sample_products: list[Product]) -> None:
    """Test GROUP BY with a single field."""
    stats = (
        await Product.objects()
        .values("category")
        .annotate(
            count=Count(),
        )
        .exec()
    )

    assert len(stats) == 3

    # Convert to dict for easier lookup
    stats_by_category = {s["category"]: s for s in stats}

    assert stats_by_category["Electronics"]["count"] == 3
    assert stats_by_category["Furniture"]["count"] == 2
    assert stats_by_category["Books"]["count"] == 2


@pytest.mark.integration
async def test_group_by_with_sum(sample_products: list[Product]) -> None:
    """Test GROUP BY with Sum aggregation."""
    stats = (
        await Product.objects()
        .values("category")
        .annotate(
            total_stock=Sum("stock"),
        )
        .exec()
    )

    stats_by_category = {s["category"]: s for s in stats}

    assert stats_by_category["Electronics"]["total_stock"] == 50  # 10 + 25 + 15
    assert stats_by_category["Furniture"]["total_stock"] == 70  # 50 + 20
    assert stats_by_category["Books"]["total_stock"] == 180  # 100 + 80


@pytest.mark.integration
async def test_group_by_with_multiple_aggregations(sample_products: list[Product]) -> None:
    """Test GROUP BY with multiple aggregations."""
    stats = (
        await Product.objects()
        .values("category")
        .annotate(
            count=Count(),
            total_stock=Sum("stock"),
            avg_price=Avg("price"),
            min_price=Min("price"),
            max_price=Max("price"),
        )
        .exec()
    )

    stats_by_category = {s["category"]: s for s in stats}

    electronics = stats_by_category["Electronics"]
    assert electronics["count"] == 3
    assert electronics["total_stock"] == 50
    assert abs(electronics["avg_price"] - (999.99 + 499.99 + 399.99) / 3) < 0.01
    assert abs(electronics["min_price"] - 399.99) < 0.01
    assert abs(electronics["max_price"] - 999.99) < 0.01


@pytest.mark.integration
async def test_group_by_with_filter(sample_products: list[Product]) -> None:
    """Test GROUP BY with filter."""
    stats = await Product.objects().filter(stock__gte=20).values("category").annotate(count=Count()).exec()

    stats_by_category = {s["category"]: s for s in stats}

    # Electronics: only Phone (25) and Tablet (15 < 20 excluded)
    # Actually Tablet has 15, Phone has 25, Laptop has 10
    # So only Phone qualifies from Electronics
    assert stats_by_category.get("Electronics", {}).get("count", 0) == 1  # Phone only
    assert stats_by_category["Furniture"]["count"] == 2  # Chair (50), Desk (20)
    assert stats_by_category["Books"]["count"] == 2  # Both books have >= 20 stock


@pytest.mark.integration
async def test_annotate_without_values_group_all(sample_products: list[Product]) -> None:
    """Test annotate() without values() (GROUP ALL)."""
    stats = (
        await Product.objects()
        .annotate(
            total_count=Count(),
            total_stock=Sum("stock"),
        )
        .exec()
    )

    assert len(stats) == 1
    assert stats[0]["total_count"] == 7
    assert stats[0]["total_stock"] == 300  # Sum of all stock


# ==================== Transaction Tests ====================


@pytest.mark.integration
async def test_transaction_commit() -> None:
    """Test that transaction commits successfully."""
    async with await SurrealDBConnectionManager.transaction() as tx:
        p1 = Product(name="TxProduct1", category="Test", price=10.0, stock=5)
        p2 = Product(name="TxProduct2", category="Test", price=20.0, stock=10)
        await p1.save(tx=tx)
        await p2.save(tx=tx)

    # Verify products were created
    count = await Product.objects().filter(category="Test").count()
    assert count == 2


@pytest.mark.integration
async def test_transaction_rollback() -> None:
    """Test that transaction rollbacks on exception."""
    try:
        async with await SurrealDBConnectionManager.transaction() as tx:
            p1 = Product(name="RollbackProduct1", category="Rollback", price=10.0, stock=5)
            await p1.save(tx=tx)
            # Force an error
            raise ValueError("Forced rollback")
    except ValueError:
        pass

    # Verify product was NOT created
    count = await Product.objects().filter(category="Rollback").count()
    assert count == 0


@pytest.mark.integration
async def test_model_transaction_classmethod() -> None:
    """Test Model.transaction() class method."""
    async with await Product.transaction() as tx:
        p = Product(name="ClassMethodProduct", category="ClassMethod", price=15.0, stock=3)
        await p.save(tx=tx)

    count = await Product.objects().filter(category="ClassMethod").count()
    assert count == 1
