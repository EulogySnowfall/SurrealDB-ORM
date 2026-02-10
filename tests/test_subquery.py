"""Tests for Subquery class — v0.11.0."""

from typing import AsyncGenerator

import pytest

from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.subquery import Subquery
from src.surreal_orm.q import Q
from src.surreal_orm.aggregations import Count


# ── Test models ──────────────────────────────────────────────────────────────


class User(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="users")
    id: str | None = None
    name: str = ""
    age: int = 0
    role: str = "user"
    is_active: bool = True


class Order(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="orders")
    id: str | None = None
    user_id: str = ""
    total: float = 0.0
    status: str = "pending"


# ==================== Unit Tests ====================


class TestSubqueryCreation:
    """Test Subquery construction and basic properties."""

    def test_create_from_queryset(self) -> None:
        qs = User.objects().filter(is_active=True)
        sq = Subquery(qs)
        assert sq.queryset is qs

    def test_repr(self) -> None:
        qs = User.objects().filter(is_active=True)
        sq = Subquery(qs)
        r = repr(sq)
        assert r.startswith("Subquery(")

    def test_eq_same_queryset(self) -> None:
        qs = User.objects()
        sq1 = Subquery(qs)
        sq2 = Subquery(qs)
        assert sq1 == sq2

    def test_eq_different_queryset(self) -> None:
        sq1 = Subquery(User.objects())
        sq2 = Subquery(User.objects())
        assert sq1 != sq2

    def test_eq_not_subquery(self) -> None:
        sq = Subquery(User.objects())
        assert sq != "not a subquery"

    def test_hash(self) -> None:
        qs = User.objects()
        sq = Subquery(qs)
        assert isinstance(hash(sq), int)


class TestSubqueryToSurql:
    """Test Subquery.to_surql() compilation."""

    def test_basic_select_all(self) -> None:
        qs = User.objects()
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        result = sq.to_surql(variables, counter)
        assert result == "(SELECT * FROM users)"
        assert variables == {}

    def test_select_specific_fields(self) -> None:
        qs = User.objects().select("id", "name")
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        result = sq.to_surql(variables, counter)
        assert result == "(SELECT id, name FROM users)"

    def test_with_filter(self) -> None:
        qs = User.objects().filter(is_active=True)
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        result = sq.to_surql(variables, counter)
        assert result == "(SELECT * FROM users WHERE is_active = $_f0)"
        assert variables == {"_f0": True}

    def test_with_multiple_filters(self) -> None:
        qs = User.objects().filter(is_active=True, age__gte=18)
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        result = sq.to_surql(variables, counter)
        assert "is_active = $_f0" in result
        assert "age >= $_f1" in result
        assert variables == {"_f0": True, "_f1": 18}

    def test_with_order_by(self) -> None:
        qs = User.objects().order_by("-age")
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        result = sq.to_surql(variables, counter)
        assert "ORDER BY age DESC" in result

    def test_with_limit(self) -> None:
        qs = User.objects().limit(10)
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        result = sq.to_surql(variables, counter)
        assert "LIMIT 10" in result

    def test_with_offset(self) -> None:
        qs = User.objects().offset(5)
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        result = sq.to_surql(variables, counter)
        assert "START 5" in result

    def test_full_query(self) -> None:
        qs = User.objects().filter(is_active=True).select("id").order_by("-age").limit(10).offset(5)
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        result = sq.to_surql(variables, counter)
        assert result == "(SELECT VALUE id FROM users WHERE is_active = $_f0 ORDER BY age DESC LIMIT 10 START 5)"
        assert variables == {"_f0": True}

    def test_variable_remapping_with_nonzero_counter(self) -> None:
        """Inner subquery variables start from the outer query's current counter."""
        qs = User.objects().filter(is_active=True, role="admin")
        sq = Subquery(qs)
        variables: dict = {}
        counter = [5]  # Simulate outer query already used 0-4
        result = sq.to_surql(variables, counter)
        assert "_f5" in variables
        assert "_f6" in variables
        assert counter[0] == 7
        assert "$_f5" in result
        assert "$_f6" in result


class TestSubqueryInFilter:
    """Test Subquery integration with QuerySet.filter()."""

    def test_filter_in_subquery(self) -> None:
        inner = User.objects().filter(is_active=True).select("id")
        qs = Order.objects().filter(user_id__in=Subquery(inner))
        query = qs._compile_query()
        assert "user_id IN (SELECT VALUE id FROM users WHERE is_active = $_f0)" in query
        assert qs._variables["_f0"] is True

    def test_filter_exact_subquery(self) -> None:
        inner = Order.objects().filter(status="completed").select("total").limit(1)
        qs = Order.objects().filter(total=Subquery(inner))
        query = qs._compile_query()
        assert "total = array::first((SELECT VALUE total FROM orders WHERE status = $_f0 LIMIT 1))" in query
        assert qs._variables["_f0"] == "completed"

    def test_filter_mixed_subquery_and_regular(self) -> None:
        """Subquery filter + regular filter share the same counter."""
        inner = User.objects().filter(role="admin").select("id")
        qs = Order.objects().filter(
            status="pending",
            user_id__in=Subquery(inner),
        )
        query = qs._compile_query()
        # Regular filter gets _f0, subquery inner filter gets _f1
        assert "$_f0" in query
        assert "$_f1" in query
        assert qs._variables["_f0"] == "pending"
        assert qs._variables["_f1"] == "admin"

    def test_filter_subquery_with_q_object(self) -> None:
        """Subquery inside a Q object filter."""
        inner = User.objects().filter(is_active=True).select("id")
        qs = Order.objects().filter(
            Q(user_id__in=Subquery(inner)) | Q(status="vip"),
        )
        query = qs._compile_query()
        assert "user_id IN (SELECT VALUE id FROM users" in query
        assert " OR " in query

    def test_nested_subquery(self) -> None:
        """Subquery whose inner QuerySet also has a subquery filter."""
        innermost = User.objects().filter(role="admin").select("id")
        middle = Order.objects().filter(user_id__in=Subquery(innermost)).select("id")
        outer = Order.objects().filter(id__in=Subquery(middle))
        query = outer._compile_query()
        # Should have nested parenthesized sub-SELECTs (VALUE for single-field)
        assert query.count("(SELECT VALUE") == 2
        # The only leaf filter value is role="admin" → _f0
        assert outer._variables["_f0"] == "admin"

    def test_subquery_with_q_inside(self) -> None:
        """Inner QuerySet uses Q objects."""
        inner = (
            User.objects()
            .filter(
                Q(role="admin") | Q(role="moderator"),
            )
            .select("id")
        )
        qs = Order.objects().filter(user_id__in=Subquery(inner))
        query = qs._compile_query()
        assert "SELECT VALUE id FROM users" in query
        assert "(role = $_f0 OR role = $_f1)" in query
        assert qs._variables["_f0"] == "admin"
        assert qs._variables["_f1"] == "moderator"


class TestSubqueryInAnnotate:
    """Test Subquery usage in annotate()."""

    def test_annotate_accepts_subquery(self) -> None:
        inner = Order.objects().select("count()")
        qs = User.objects().annotate(order_count=Subquery(inner))
        assert "order_count" in qs._annotations
        assert isinstance(qs._annotations["order_count"], Subquery)

    def test_annotate_mixed_aggregation_and_subquery(self) -> None:
        inner = Order.objects().select("count()")
        qs = (
            User.objects()
            .values("role")
            .annotate(
                user_count=Count(),
                sample_orders=Subquery(inner),
            )
        )
        assert isinstance(qs._annotations["user_count"], Count)
        assert isinstance(qs._annotations["sample_orders"], Subquery)


class TestSubqueryExport:
    """Test that Subquery is properly exported."""

    def test_import_from_surreal_orm(self) -> None:
        from src.surreal_orm import Subquery as SubqueryImport

        assert SubqueryImport is Subquery

    def test_in_all(self) -> None:
        import src.surreal_orm as orm

        assert "Subquery" in orm.__all__


# ==================== Integration Tests ====================


@pytest.fixture(scope="module", autouse=True)
async def _setup_connection() -> AsyncGenerator[None, None]:
    """Set up ORM connection for integration tests."""
    from src.surreal_orm import SurrealDBConnectionManager

    SurrealDBConnectionManager.set_connection(
        "http://localhost:8001",
        "root",
        "root",
        "test",
        "test_subquery",
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


@pytest.mark.integration
class TestSubqueryIntegration:
    """Integration tests requiring a live SurrealDB instance."""

    @pytest.fixture(autouse=True)
    async def setup_data(self) -> None:
        """Create test data for subquery integration tests."""
        from src.surreal_orm import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()

        # Clean up
        await client.query("DELETE FROM users;")
        await client.query("DELETE FROM orders;")

        # Create users
        await client.query("CREATE users:alice SET name = 'Alice', age = 30, role = 'admin', is_active = true;")
        await client.query("CREATE users:bob SET name = 'Bob', age = 25, role = 'user', is_active = true;")
        await client.query("CREATE users:charlie SET name = 'Charlie', age = 35, role = 'user', is_active = false;")

        # Create orders
        await client.query("CREATE orders:o1 SET user_id = 'users:alice', total = 100.0, status = 'completed';")
        await client.query("CREATE orders:o2 SET user_id = 'users:bob', total = 50.0, status = 'pending';")
        await client.query("CREATE orders:o3 SET user_id = 'users:alice', total = 200.0, status = 'completed';")

    async def test_filter_by_subquery_in(self) -> None:
        """Filter orders where status is in a subquery result set."""
        # Use status field (string) to avoid record-ID type mismatch
        completed_statuses = Order.objects().filter(total__gte=100).select("status")
        orders = (
            await Order.objects()
            .filter(
                status__in=Subquery(completed_statuses),
            )
            .exec()
        )
        # orders:o1 and orders:o3 have total >= 100 and status='completed'
        # So status IN (SELECT status ...) matches all 'completed' orders
        assert len(orders) >= 2

    async def test_filter_by_subquery_scalar(self) -> None:
        """Filter using a scalar subquery (e.g., max total) with array::first()."""
        completed_orders = Order.objects().filter(status="completed").select("total").order_by("-total").limit(1)
        orders = (
            await Order.objects()
            .filter(
                total=Subquery(completed_orders),
            )
            .exec()
        )
        # array::first() extracts the single value (200.0) from the subquery
        assert len(orders) >= 1
