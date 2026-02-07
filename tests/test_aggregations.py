"""Tests for ORM v0.3.0 features: aggregations and GROUP BY."""

import pytest
from pydantic import Field
from src.surreal_orm.model_base import BaseSurrealModel
from src.surreal_orm.aggregations import (
    Aggregation,
    Count,
    Sum,
    Avg,
    Min,
    Max,
)


class Order(BaseSurrealModel):
    """Test model for aggregation tests."""

    id: str | None = None
    status: str = Field(...)
    amount: float = Field(..., ge=0)
    customer_id: str = Field(...)


# ==================== Aggregation Classes Tests ====================


def test_count_to_surql() -> None:
    """Test Count aggregation SurrealQL generation."""
    count = Count()
    assert count.to_surql("total") == "count() AS total"
    assert count.function_name == "count"


def test_sum_to_surql() -> None:
    """Test Sum aggregation SurrealQL generation."""
    total = Sum("amount")
    assert total.to_surql("total_amount") == "math::sum(amount) AS total_amount"
    assert total.function_name == "math::sum"
    assert total.field == "amount"


def test_avg_to_surql() -> None:
    """Test Avg aggregation SurrealQL generation."""
    avg = Avg("price")
    assert avg.to_surql("avg_price") == "math::mean(price) AS avg_price"
    assert avg.function_name == "math::mean"
    assert avg.field == "price"


def test_min_to_surql() -> None:
    """Test Min aggregation SurrealQL generation."""
    minimum = Min("age")
    assert minimum.to_surql("youngest") == "math::min(age) AS youngest"
    assert minimum.function_name == "math::min"
    assert minimum.field == "age"


def test_max_to_surql() -> None:
    """Test Max aggregation SurrealQL generation."""
    maximum = Max("score")
    assert maximum.to_surql("highest") == "math::max(score) AS highest"
    assert maximum.function_name == "math::max"
    assert maximum.field == "score"


def test_aggregation_is_abstract() -> None:
    """Test that Aggregation is an abstract base class."""
    with pytest.raises(TypeError):
        Aggregation()  # type: ignore


# ==================== QuerySet values() and annotate() Tests ====================


def test_queryset_values_stores_fields() -> None:
    """Test that values() stores the GROUP BY fields."""
    qs = Order.objects().values("status", "customer_id")
    assert qs._group_by_fields == ["status", "customer_id"]


def test_queryset_values_is_chainable() -> None:
    """Test that values() returns self for chaining."""
    qs = Order.objects()
    result = qs.values("status")
    assert result is qs


def test_queryset_annotate_stores_aggregations() -> None:
    """Test that annotate() stores the aggregations."""
    qs = Order.objects().annotate(
        count=Count(),
        total=Sum("amount"),
    )
    assert "count" in qs._annotations
    assert "total" in qs._annotations
    assert isinstance(qs._annotations["count"], Count)
    assert isinstance(qs._annotations["total"], Sum)


def test_queryset_annotate_is_chainable() -> None:
    """Test that annotate() returns self for chaining."""
    qs = Order.objects()
    result = qs.annotate(count=Count())
    assert result is qs


def test_queryset_values_annotate_chain() -> None:
    """Test that values() and annotate() can be chained."""
    qs = (
        Order.objects()
        .values("status")
        .annotate(
            count=Count(),
            total=Sum("amount"),
            avg_amount=Avg("amount"),
        )
    )
    assert qs._group_by_fields == ["status"]
    assert len(qs._annotations) == 3


def test_queryset_values_annotate_with_filter() -> None:
    """Test that values(), annotate() and filter() can be chained."""
    qs = Order.objects().filter(amount__gt=100).values("status").annotate(count=Count())
    assert qs._filters == [("amount", "gt", 100)]
    assert qs._group_by_fields == ["status"]
    assert "count" in qs._annotations


def test_queryset_multiple_group_fields() -> None:
    """Test GROUP BY with multiple fields."""
    qs = (
        Order.objects()
        .values("status", "customer_id")
        .annotate(
            count=Count(),
            total=Sum("amount"),
        )
    )
    assert qs._group_by_fields == ["status", "customer_id"]


def test_queryset_annotate_without_values() -> None:
    """Test annotate() without values() (GROUP ALL behavior)."""
    qs = Order.objects().annotate(
        count=Count(),
        total=Sum("amount"),
    )
    assert qs._group_by_fields == []
    assert len(qs._annotations) == 2


# ==================== QuerySet _compile_where_clause() Tests ====================


def test_compile_where_clause_empty() -> None:
    """Test _compile_where_clause with no filters."""
    qs = Order.objects()
    assert qs._compile_where_clause() == ""


def test_compile_where_clause_single_filter() -> None:
    """Test _compile_where_clause with single filter."""
    qs = Order.objects().filter(status="paid")
    where = qs._compile_where_clause()
    assert "WHERE" in where
    assert "status" in where
    assert "$_f0" in where
    assert qs._variables["_f0"] == "paid"


def test_compile_where_clause_multiple_filters() -> None:
    """Test _compile_where_clause with multiple filters."""
    qs = Order.objects().filter(status="paid", amount__gt=100)
    where = qs._compile_where_clause()
    assert "WHERE" in where
    assert "AND" in where


def test_compile_where_clause_in_operator() -> None:
    """Test _compile_where_clause with IN operator."""
    qs = Order.objects().filter(status__in=["paid", "pending"])
    where = qs._compile_where_clause()
    assert "IN" in where
    assert "$_f0" in where
    assert qs._variables["_f0"] == ["paid", "pending"]


# ==================== Import/Export Tests ====================


def test_aggregations_exported_from_init() -> None:
    """Test that aggregation classes are exported from surreal_orm."""
    from src.surreal_orm import Count, Sum, Avg, Min, Max, Aggregation

    assert Count is not None
    assert Sum is not None
    assert Avg is not None
    assert Min is not None
    assert Max is not None
    assert Aggregation is not None


def test_version_exists() -> None:
    """Test that version string exists and is valid."""
    from src.surreal_orm import __version__

    assert __version__ is not None
    assert len(__version__) > 0
    # Version should be in semver format
    parts = __version__.split(".")
    assert len(parts) >= 2


# ==================== Model Transaction Tests (Unit) ====================


def test_model_save_accepts_tx_parameter() -> None:
    """Test that save() method accepts tx parameter."""
    import inspect

    sig = inspect.signature(Order.save)
    assert "tx" in sig.parameters
    assert sig.parameters["tx"].default is None


def test_model_update_accepts_tx_parameter() -> None:
    """Test that update() method accepts tx parameter."""
    import inspect

    sig = inspect.signature(Order.update)
    assert "tx" in sig.parameters
    assert sig.parameters["tx"].default is None


def test_model_merge_accepts_tx_parameter() -> None:
    """Test that merge() method accepts tx parameter."""
    import inspect

    sig = inspect.signature(Order.merge)
    assert "tx" in sig.parameters
    assert sig.parameters["tx"].default is None


def test_model_delete_accepts_tx_parameter() -> None:
    """Test that delete() method accepts tx parameter."""
    import inspect

    sig = inspect.signature(Order.delete)
    assert "tx" in sig.parameters
    assert sig.parameters["tx"].default is None


def test_model_has_transaction_classmethod() -> None:
    """Test that Model has transaction() class method."""
    assert hasattr(Order, "transaction")
    assert callable(getattr(Order, "transaction"))


def test_connection_manager_has_transaction_method() -> None:
    """Test that SurrealDBConnectionManager has transaction() method."""
    from src.surreal_orm import SurrealDBConnectionManager

    assert hasattr(SurrealDBConnectionManager, "transaction")
    assert callable(getattr(SurrealDBConnectionManager, "transaction"))
