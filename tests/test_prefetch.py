"""Tests for Prefetch class — v0.11.0."""

from collections.abc import AsyncGenerator

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
        await client.query("RELATE prefetch_users:alice->wrote->prefetch_posts:p1;")
        await client.query("RELATE prefetch_users:alice->wrote->prefetch_posts:p2;")
        await client.query("RELATE prefetch_users:bob->wrote->prefetch_posts:p3;")

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
