"""Tests for Prefetch class — v0.11.0."""

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.prefetch import Prefetch

# ── Test models ──────────────────────────────────────────────────────────────


class PrefetchUser(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="prefetch_users")
    id: str | None = None
    name: str = ""


class PrefetchPost(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="prefetch_posts")
    id: str | None = None
    title: str = ""
    published: bool = False


# ==================== Unit Tests ====================


class TestPrefetchCreation:
    """Test Prefetch construction and defaults."""

    def test_defaults(self) -> None:
        p = Prefetch("posts")
        assert p.relation_name == "posts"
        assert p.queryset is None
        assert p.to_attr == "posts"

    def test_custom_to_attr(self) -> None:
        p = Prefetch("posts", to_attr="recent_posts")
        assert p.to_attr == "recent_posts"

    def test_custom_queryset(self) -> None:
        qs = PrefetchPost.objects().filter(published=True)
        p = Prefetch("posts", queryset=qs)
        assert p.queryset is qs

    def test_repr_simple(self) -> None:
        p = Prefetch("posts")
        assert repr(p) == "Prefetch('posts')"

    def test_repr_with_to_attr(self) -> None:
        p = Prefetch("posts", to_attr="my_posts")
        r = repr(p)
        assert "to_attr='my_posts'" in r

    def test_repr_with_queryset(self) -> None:
        qs = PrefetchPost.objects()
        p = Prefetch("posts", queryset=qs)
        r = repr(p)
        assert "queryset=" in r

    def test_eq_same(self) -> None:
        qs = PrefetchPost.objects()
        p1 = Prefetch("posts", queryset=qs, to_attr="x")
        p2 = Prefetch("posts", queryset=qs, to_attr="x")
        assert p1 == p2

    def test_eq_different(self) -> None:
        p1 = Prefetch("posts")
        p2 = Prefetch("comments")
        assert p1 != p2

    def test_eq_not_prefetch(self) -> None:
        p = Prefetch("posts")
        assert p != "posts"

    def test_hash(self) -> None:
        p = Prefetch("posts")
        assert isinstance(hash(p), int)


class TestPrefetchRelatedMethod:
    """Test QuerySet.prefetch_related() with strings and Prefetch objects."""

    def test_strings(self) -> None:
        qs = PrefetchUser.objects().prefetch_related("posts", "comments")
        assert qs._prefetch_related == ["posts", "comments"]

    def test_prefetch_objects(self) -> None:
        p = Prefetch("posts", to_attr="my_posts")
        qs = PrefetchUser.objects().prefetch_related(p)
        assert qs._prefetch_related == [p]

    def test_mixed(self) -> None:
        p = Prefetch("posts", to_attr="recent")
        qs = PrefetchUser.objects().prefetch_related("comments", p)
        assert len(qs._prefetch_related) == 2
        assert qs._prefetch_related[0] == "comments"
        assert isinstance(qs._prefetch_related[1], Prefetch)


def _prefetch_client() -> Any:
    from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

    client = AsyncMock()
    client.query = AsyncMock(return_value=QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=[])]))
    return client


async def _run_prefetch(custom_qs: Any) -> tuple[str, dict[str, Any]]:
    """Run ``_execute_prefetch()`` for one parent and return the (sql, variables) it sent."""
    qs = PrefetchUser.objects().prefetch_related(Prefetch("wrote", queryset=custom_qs, to_attr="tagged"))
    client = _prefetch_client()
    with patch(
        "src.surreal_orm.query_set.SurrealDBConnectionManager.get_client",
        new_callable=AsyncMock,
        return_value=client,
    ):
        await qs._execute_prefetch([PrefetchUser(id="alice", name="Alice")])
    client.query.assert_awaited_once()
    sql, variables = client.query.await_args.args
    return sql, variables


class TestPrefetchDeclaredVariables:
    """#344: the variables declared on a Prefetch queryset are sent with the prefetch query."""

    async def test_declared_variable_is_sent(self) -> None:
        sql, variables = await _run_prefetch(PrefetchPost.objects().filter(tag="$t").variables(t="x"))
        assert sql == "SELECT in, out.* FROM wrote WHERE in IN [prefetch_users:alice] AND tag = $t;"
        assert variables == {"t": "x"}

    async def test_declared_and_compiled_variables_are_both_sent(self) -> None:
        custom = PrefetchPost.objects().filter(published=True, tag="$t").variables(t="x")
        sql, variables = await _run_prefetch(custom)
        assert sql.endswith("AND published = $_f0 AND tag = $t;")
        assert variables == {"_f0": True, "t": "x"}

    async def test_declared_variable_not_referenced_is_still_sent(self) -> None:
        # Same rule as Subquery: every declared name goes out with the query.
        _, variables = await _run_prefetch(PrefetchPost.objects().filter(published=True).variables(t="x"))
        assert variables == {"_f0": True, "t": "x"}

    async def test_no_declared_variables_sends_the_same_bindings(self) -> None:
        sql, variables = await _run_prefetch(PrefetchPost.objects().filter(published=True))
        assert sql == "SELECT in, out.* FROM wrote WHERE in IN [prefetch_users:alice] AND published = $_f0;"
        assert variables == {"_f0": True}

    async def test_stale_bindings_of_an_earlier_compile_are_not_sent(self) -> None:
        custom = PrefetchPost.objects().filter(published=True).nearby("location", (1.0, 2.0), max_distance=5).variables(t="x")
        custom._compile_query()
        assert "_geo_max" in custom._variables
        _, variables = await _run_prefetch(custom)
        assert variables == {"_f0": True, "t": "x"}

    async def test_compiled_binding_wins_on_a_name_clash(self) -> None:
        # .variables() refuses names starting with "_", so this state can only be
        # built by hand; the ORM's own binding must still never be replaced.
        custom = PrefetchPost.objects().filter(published=True)
        custom._variables = {"_f0": "declared"}
        custom._declared_variables = frozenset({"_f0"})
        _, variables = await _run_prefetch(custom)
        assert variables == {"_f0": True}


class TestPrefetchExport:
    """Test that Prefetch is properly exported."""

    def test_import_from_surreal_orm(self) -> None:
        from src.surreal_orm import Prefetch as PrefetchImport

        assert PrefetchImport is Prefetch

    def test_in_all(self) -> None:
        import src.surreal_orm as orm

        assert "Prefetch" in orm.__all__


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
        "test_prefetch",
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


@pytest.mark.integration
class TestPrefetchIntegration:
    """Integration tests requiring a live SurrealDB instance."""

    @pytest.fixture(autouse=True)
    async def setup_data(self) -> None:
        """Create test data with graph relations."""
        from src.surreal_orm import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()

        # Clean up
        await client.query("DELETE FROM prefetch_users;")
        await client.query("DELETE FROM prefetch_posts;")
        await client.query("DELETE FROM wrote;")

        # Create users
        await client.query("CREATE prefetch_users:alice SET name = 'Alice';")
        await client.query("CREATE prefetch_users:bob SET name = 'Bob';")

        # Create posts
        await client.query("CREATE prefetch_posts:p1 SET title = 'Post 1', published = true;")
        await client.query("CREATE prefetch_posts:p2 SET title = 'Post 2', published = false;")
        await client.query("CREATE prefetch_posts:p3 SET title = 'Post 3', published = true;")

        # Create graph relations: user -wrote-> post
        await client.query("RELATE prefetch_users:alice->wrote->prefetch_posts:p1 SET tag = 'x';")
        await client.query("RELATE prefetch_users:alice->wrote->prefetch_posts:p2 SET tag = 'y';")
        await client.query("RELATE prefetch_users:bob->wrote->prefetch_posts:p3 SET tag = 'y';")

    async def test_prefetch_graph_relation(self) -> None:
        """Prefetch graph relation results as batch."""
        users = await PrefetchUser.objects().prefetch_related("wrote").exec()
        assert len(users) >= 2

        for user in users:
            assert hasattr(user, "wrote")
            wrote = user.wrote
            assert isinstance(wrote, list)

        # Alice has 2 posts, Bob has 1
        alice = next(u for u in users if u.name == "Alice")
        bob = next(u for u in users if u.name == "Bob")
        assert len(alice.wrote) == 2
        assert len(bob.wrote) == 1

    async def test_prefetch_with_prefetch_object(self) -> None:
        """Prefetch with Prefetch object and custom to_attr."""
        users = (
            await PrefetchUser.objects()
            .prefetch_related(
                Prefetch("wrote", to_attr="written_posts"),
            )
            .exec()
        )

        for user in users:
            assert hasattr(user, "written_posts")

    async def test_prefetch_with_custom_queryset(self) -> None:
        """Prefetch with custom QuerySet filters on the edge query."""
        # This tests that the custom queryset's filters are applied
        users = (
            await PrefetchUser.objects()
            .prefetch_related(
                Prefetch(
                    "wrote",
                    queryset=PrefetchPost.objects().filter(published=True),
                    to_attr="published_posts",
                ),
            )
            .exec()
        )

        for user in users:
            assert hasattr(user, "published_posts")

    async def test_prefetch_queryset_declared_variables_are_bound(self) -> None:
        """#344: a declared variable in a Prefetch queryset filter is sent and filters the edges."""
        users = (
            await PrefetchUser.objects()
            .prefetch_related(
                Prefetch(
                    "wrote",
                    queryset=PrefetchPost.objects().filter(tag="$t").variables(t="x"),
                    to_attr="tagged",
                ),
            )
            .exec()
        )

        by_name = {u.name: u for u in users}
        assert [str(edge["out"]["id"]) for edge in by_name["Alice"].tagged] == ["prefetch_posts:p1"]
        assert by_name["Bob"].tagged == []
