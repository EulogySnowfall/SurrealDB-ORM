"""Tests for Subquery class — v0.11.0."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm.aggregations import Count
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.q import Q
from src.surreal_orm.subquery import Subquery

# The SurrealDB 3.2.x sub-SELECT regression (#147) is non-deterministic: a given
# execution is corrupted ~75% of the time.  Repeating the probe makes detection
# effectively certain (0.25**12 ≈ 6e-8 chance of a false pass) while staying
# fast on unaffected versions.  Verified affected: 3.2.0, 3.2.1, 3.2.2, 3.2.3.
# Verified unaffected: 2.6.5, 3.1.3, 3.1.5 (the pinned version).
_UNSTABLE_PROBE_REPEATS = 12

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


class TestSubqueryLetHoisting:
    """Uncorrelated subqueries are hoisted into a ``LET`` prelude — v0.32.0.

    SurrealDB 3.2.x evaluates an inline uncorrelated sub-SELECT once per outer
    row and shares its LIMIT budget across those evaluations, so a subquery
    combining ``ORDER BY`` and ``LIMIT`` yields its value for only some rows
    (issue #147).  Binding the subquery to a ``LET`` variable evaluates it
    exactly once, which is correct on every supported version.
    """

    def test_compile_query_emits_let_prelude(self) -> None:
        inner = User.objects().filter(is_active=True).select("id")
        qs = Order.objects().filter(user_id__in=Subquery(inner))
        query = qs._compile_query()
        assert query.startswith("LET $_sq0 = (SELECT VALUE id FROM users WHERE is_active = $_f0);")
        assert "user_id IN $_sq0" in query
        # The inline sub-SELECT must no longer appear in the outer statement.
        assert "IN (SELECT" not in query

    def test_scalar_subquery_wraps_the_let_variable(self) -> None:
        inner = Order.objects().filter(status="completed").select("total").order_by("-total").limit(1)
        qs = Order.objects().filter(total=Subquery(inner))
        query = qs._compile_query()
        assert "LET $_sq0 = (SELECT VALUE total FROM orders WHERE status = $_f0 ORDER BY total DESC LIMIT 1);" in query
        assert "total = array::first($_sq0)" in query

    def test_prelude_precedes_the_select_statement(self) -> None:
        inner = User.objects().filter(is_active=True).select("id")
        qs = Order.objects().filter(user_id__in=Subquery(inner))
        query = qs._compile_query()
        assert query.index("LET $_sq0") < query.index("SELECT * FROM orders")

    def test_nested_subqueries_hoist_innermost_first(self) -> None:
        """A subquery referencing another must be bound after the one it uses."""
        innermost = User.objects().filter(role="admin").select("id")
        middle = Order.objects().filter(user_id__in=Subquery(innermost)).select("id")
        outer = Order.objects().filter(id__in=Subquery(middle))
        query = outer._compile_query()
        assert query.count("LET $_sq") == 2
        # $_sq0 is the innermost; $_sq1 references it and must come later.
        assert query.index("LET $_sq0") < query.index("LET $_sq1")
        assert "user_id IN $_sq0" in query
        assert "id IN $_sq1" in query

    def test_multiple_independent_subqueries_get_distinct_variables(self) -> None:
        a = User.objects().filter(role="admin").select("id")
        b = User.objects().filter(role="moderator").select("id")
        qs = Order.objects().filter(user_id__in=Subquery(a), id__in=Subquery(b))
        query = qs._compile_query()
        assert "LET $_sq0" in query and "LET $_sq1" in query
        assert "user_id IN $_sq0" in query
        assert "id IN $_sq1" in query

    def test_to_surql_without_prelude_stays_inline(self) -> None:
        """LIVE SELECT has no prelude to hoist into, so inline must still work."""
        qs = User.objects().filter(is_active=True).select("id")
        sq = Subquery(qs)
        variables: dict = {}
        counter = [0]
        assert sq.to_surql(variables, counter) == "(SELECT VALUE id FROM users WHERE is_active = $_f0)"

    def test_live_query_where_clause_stays_inline(self) -> None:
        """`live()` cannot emit a LET prelude — its WHERE must remain self-contained."""
        inner = User.objects().filter(is_active=True).select("id")
        qs = Order.objects().filter(user_id__in=Subquery(inner))
        where_parts, _ = qs._build_where_parts()
        rendered = " AND ".join(where_parts)
        assert "(SELECT VALUE id FROM users" in rendered
        assert "$_sq" not in rendered

    def test_annotate_subquery_is_hoisted(self) -> None:
        """`annotate()` compiles through its own path and must hoist too."""
        inner = Order.objects().filter(status="completed").select("total").order_by("-total").limit(1)
        qs = User.objects().values("role").annotate(top_total=Subquery(inner))
        query = qs._compile_annotate_query()
        assert query.startswith("LET $_sq0 = (SELECT VALUE total FROM orders")
        assert "$_sq0 AS top_total" in query
        assert query.index("LET $_sq0") < query.index("SELECT role")

    def test_annotate_without_subquery_has_no_prelude(self) -> None:
        qs = User.objects().values("role").annotate(n=Count())
        query = qs._compile_annotate_query()
        assert not query.startswith("LET ")
        assert "count() AS n" in query

    def test_query_without_subquery_has_no_prelude(self) -> None:
        qs = Order.objects().filter(status="pending")
        query = qs._compile_query()
        assert not query.startswith("LET ")
        assert query.startswith("SELECT * FROM orders")


class TestSubqueryInFilter:
    """Test Subquery integration with QuerySet.filter()."""

    def test_filter_in_subquery(self) -> None:
        inner = User.objects().filter(is_active=True).select("id")
        qs = Order.objects().filter(user_id__in=Subquery(inner))
        query = qs._compile_query()
        # v0.32.0: uncorrelated subqueries are hoisted into a LET prelude.
        assert "LET $_sq0 = (SELECT VALUE id FROM users WHERE is_active = $_f0);" in query
        assert "user_id IN $_sq0" in query
        assert qs._variables["_f0"] is True

    def test_filter_exact_subquery(self) -> None:
        inner = Order.objects().filter(status="completed").select("total").limit(1)
        qs = Order.objects().filter(total=Subquery(inner))
        query = qs._compile_query()
        assert "LET $_sq0 = (SELECT VALUE total FROM orders WHERE status = $_f0 LIMIT 1);" in query
        assert "total = array::first($_sq0)" in query
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
        assert "LET $_sq0 = (SELECT VALUE id FROM users" in query
        assert "user_id IN $_sq0" in query
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
    from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
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
        """Filter using a scalar subquery (e.g., max total) with array::first().

        NOTE: this is the test that fails on the SurrealDB 3.2.x line (issue #147).
        The clause-isolation tests below pinpoint *which* clause combination
        regressed, so a future monitor failure is diagnosable at a glance.
        """
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

    # ── Clause isolation for the SurrealDB 3.2.0 sub-SELECT regression (#147) ──
    #
    # 3.2.0 broke uncorrelated sub-SELECTs that combine ORDER BY *and* LIMIT
    # (still unfixed as of 3.2.3):
    # the subquery is evaluated with shared limit state across outer rows, so
    # only some rows receive a value and the rest get an empty array.  Either
    # clause alone is unaffected.  These tests split the failure surface so the
    # version monitor reports which combination regressed.

    async def test_subquery_no_order_no_limit(self) -> None:
        """Baseline: subquery with neither ORDER BY nor LIMIT."""
        inner = Order.objects().filter(status="completed").select("status")
        orders = await Order.objects().filter(status__in=Subquery(inner)).exec()
        # orders:o1 and orders:o3 are 'completed'
        assert len(orders) == 2

    async def test_subquery_limit_only(self) -> None:
        """Subquery with LIMIT but no ORDER BY — unaffected by #147."""
        inner = Order.objects().filter(status="completed").select("total").limit(1)
        orders = await Order.objects().filter(total=Subquery(inner)).exec()
        # Without ORDER BY the picked completed order is arbitrary (o1=100.0 or
        # o3=200.0), but both totals are unique across the fixture, so exactly
        # one order must match whichever was picked.
        assert len(orders) == 1
        assert orders[0].total in (100.0, 200.0)

    async def test_subquery_order_by_only(self) -> None:
        """Subquery with ORDER BY but no LIMIT — unaffected by #147."""
        inner = Order.objects().filter(status="completed").select("total").order_by("-total")
        orders = await Order.objects().filter(total=Subquery(inner)).exec()
        # array::first() of the DESC-ordered totals is 200.0 → orders:o3
        assert len(orders) == 1
        assert orders[0].total == 200.0

    @pytest.mark.xfail(
        reason="Upstream SurrealDB 3.2.x bug #147, unfixed as of 3.2.3. The ORM no "
        "longer depends on this behaviour — Subquery hoists into a LET binding "
        "— so this tracks upstream only and must not gate CI.",
        strict=False,
    )
    async def test_inline_subquery_order_by_and_limit_is_row_stable_upstream(self) -> None:
        """Upstream tracker: raw *inline* sub-SELECT with ORDER BY + LIMIT.

        This asserts SurrealDB's own behaviour, deliberately bypassing the ORM,
        and is the standalone repro for #147: the inline sub-SELECT returns
        ``[200.0]`` for some outer rows and ``[]`` for the rest, with *which*
        rows varying between identical executions (~75% of executions corrupted
        on 3.2.x, hence the repeats).

        The ORM no longer emits this shape, so a failure here is informational,
        not a defect in this repository — it means upstream is still broken.
        When SurrealDB fixes it, this xpasses and the ``LET`` hoisting in
        :meth:`Subquery.to_surql` could in principle be reconsidered.
        """
        from src.surreal_orm import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()
        sql = (
            "SELECT total, (SELECT VALUE total FROM orders "
            "WHERE status = 'completed' ORDER BY total DESC LIMIT 1) AS mx "
            "FROM orders ORDER BY total;"
        )

        observed: list[list[Any]] = []
        for _ in range(_UNSTABLE_PROBE_REPEATS):
            result = await client.query(sql)
            rows = result.results[-1].result
            assert isinstance(rows, list) and len(rows) == 3, rows
            observed.append([row["mx"] for row in rows])

        corrupted = [per_row for per_row in observed if not all(v == [200.0] for v in per_row)]
        assert not corrupted, (
            "uncorrelated sub-SELECT with ORDER BY + LIMIT returned different values "
            f"per outer row in {len(corrupted)}/{_UNSTABLE_PROBE_REPEATS} executions "
            f"(expected [200.0] on every row): {corrupted[:3]} "
            "— see issue #147 (SurrealDB 3.2.x regression)"
        )

    async def test_ormsubquery_with_order_by_and_limit_is_row_stable(self) -> None:
        """The ORM's own ORDER BY + LIMIT subquery must be correct on 3.2.x.

        This is the contract that actually matters: whatever SurrealDB does
        with inline sub-SELECTs, ``Subquery`` must resolve to the single
        highest completed total on every execution.  Repeated because the
        underlying server bug is non-deterministic — a single pass could
        succeed by luck even without the ``LET`` hoisting.
        """
        for attempt in range(_UNSTABLE_PROBE_REPEATS):
            inner = Order.objects().filter(status="completed").select("total").order_by("-total").limit(1)
            orders = await Order.objects().filter(total=Subquery(inner)).exec()
            assert len(orders) == 1, (attempt, orders)
            assert orders[0].total == 200.0, (attempt, orders)

    async def test_let_hoisting_workaround_is_row_stable(self) -> None:
        """The LET-hoisting workaround for #147 works on every supported version.

        If SurrealDB never fixes the sub-SELECT regression, ``Subquery.to_surql()``
        can emit uncorrelated subqueries as a ``LET`` prelude.  This pins that
        the escape hatch is actually viable — and it is repeated for the same
        reason as the test above, so a genuinely stable result is required.
        """
        from src.surreal_orm import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()
        sql = (
            "LET $mx = array::first((SELECT VALUE total FROM orders "
            "WHERE status = 'completed' ORDER BY total DESC LIMIT 1)); "
            "SELECT total FROM orders WHERE total = $mx;"
        )

        for attempt in range(_UNSTABLE_PROBE_REPEATS):
            result = await client.query(sql)
            rows = result.results[-1].result
            assert isinstance(rows, list) and len(rows) == 1, (attempt, rows)
            assert rows[0]["total"] == 200.0, (attempt, rows)
