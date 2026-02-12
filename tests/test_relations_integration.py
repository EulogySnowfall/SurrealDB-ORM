"""Integration tests for ORM v0.4.0 features: relations and graph traversal."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_relations"


class User(BaseSurrealModel):
    """Test model for relation tests."""

    id: str | None = None
    name: str = Field(...)
    active: bool = Field(default=True)


class Post(BaseSurrealModel):
    """Test model for relation tests."""

    id: str | None = None
    title: str = Field(...)
    content: str = Field(default="")


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
async def cleanup_tables() -> AsyncGenerator[Any, Any]:
    """Clean up tables before and after each test."""
    # Clean up before test
    client = await SurrealDBConnectionManager.get_client()
    try:
        await client.query("DELETE User;")
        await client.query("DELETE Post;")
        await client.query("DELETE follows;")
        await client.query("DELETE likes;")
    except Exception:
        pass
    yield
    # Clean up after test
    try:
        await client.query("DELETE User;")
        await client.query("DELETE Post;")
        await client.query("DELETE follows;")
        await client.query("DELETE likes;")
    except Exception:
        pass


@pytest.fixture
async def sample_users() -> list[User]:
    """Create sample users for testing."""
    users = [
        User(id="alice", name="Alice", active=True),
        User(id="bob", name="Bob", active=True),
        User(id="charlie", name="Charlie", active=True),
        User(id="david", name="David", active=False),
    ]
    for user in users:
        await user.save()
    return users


@pytest.fixture
async def sample_posts() -> list[Post]:
    """Create sample posts for testing."""
    posts = [
        Post(id="post1", title="First Post", content="Hello World"),
        Post(id="post2", title="Second Post", content="Another post"),
        Post(id="post3", title="Third Post", content="Yet another"),
    ]
    for post in posts:
        await post.save()
    return posts


# ==================== Model.relate() Tests ====================


@pytest.mark.integration
async def test_relate_creates_edge(sample_users: list[User]) -> None:
    """Test that relate() creates a graph edge."""
    alice, bob = sample_users[0], sample_users[1]

    # Create relation
    result = await alice.relate("follows", bob)

    assert result is not None
    assert "in" in result or "out" in result


@pytest.mark.integration
async def test_relate_with_edge_data(sample_users: list[User]) -> None:
    """Test relate() with edge data."""
    alice, bob = sample_users[0], sample_users[1]

    # Create relation with data
    result = await alice.relate("follows", bob, since="2025-01-01", strength="strong")

    assert result is not None


@pytest.mark.integration
async def test_relate_in_transaction(sample_users: list[User]) -> None:
    """Test relate() within a transaction."""
    alice, bob, charlie = sample_users[0], sample_users[1], sample_users[2]

    async with await SurrealDBConnectionManager.transaction() as tx:
        await alice.relate("follows", bob, tx=tx)
        await alice.relate("follows", charlie, tx=tx)

    # Verify relations were created
    following = await alice.get_related("follows", direction="out", model_class=User)
    assert len(following) == 2


@pytest.mark.integration
async def test_relate_raises_for_unsaved_source() -> None:
    """Test that relate() raises error for unsaved source."""
    alice = User(name="Alice")  # Not saved
    bob = User(id="bob", name="Bob")
    await bob.save()

    with pytest.raises(Exception) as exc:
        await alice.relate("follows", bob)

    assert "unsaved" in str(exc.value).lower()


@pytest.mark.integration
async def test_relate_raises_for_unsaved_target(sample_users: list[User]) -> None:
    """Test that relate() raises error for unsaved target."""
    alice = sample_users[0]
    eve = User(name="Eve")  # Not saved

    with pytest.raises(Exception) as exc:
        await alice.relate("follows", eve)

    assert "unsaved" in str(exc.value).lower()


# ==================== Model.get_related() Tests ====================


@pytest.mark.integration
async def test_get_related_outgoing(sample_users: list[User]) -> None:
    """Test get_related() for outgoing relations."""
    alice, bob, charlie = sample_users[0], sample_users[1], sample_users[2]

    # Create relations
    await alice.relate("follows", bob)
    await alice.relate("follows", charlie)

    # Get outgoing relations
    following = await alice.get_related("follows", direction="out", model_class=User)

    assert len(following) == 2
    following_ids = {u.id for u in following}
    assert "bob" in following_ids
    assert "charlie" in following_ids


@pytest.mark.integration
async def test_get_related_incoming(sample_users: list[User]) -> None:
    """Test get_related() for incoming relations."""
    alice, bob, charlie = sample_users[0], sample_users[1], sample_users[2]

    # Create relations where bob and charlie follow alice
    await bob.relate("follows", alice)
    await charlie.relate("follows", alice)

    # Get incoming relations (followers)
    followers = await alice.get_related("follows", direction="in", model_class=User)

    assert len(followers) == 2
    follower_ids = {u.id for u in followers}
    assert "bob" in follower_ids
    assert "charlie" in follower_ids


@pytest.mark.integration
async def test_get_related_returns_empty_when_none(sample_users: list[User]) -> None:
    """Test get_related() returns empty list when no relations."""
    alice = sample_users[0]

    following = await alice.get_related("follows", direction="out", model_class=User)

    assert following == []


@pytest.mark.integration
async def test_get_related_without_model_class(sample_users: list[User]) -> None:
    """Test get_related() returns dicts when no model_class."""
    alice, bob = sample_users[0], sample_users[1]

    await alice.relate("follows", bob)

    following = await alice.get_related("follows", direction="out")

    assert len(following) == 1
    assert isinstance(following[0], dict)


# ==================== Model.remove_relation() Tests ====================


@pytest.mark.integration
async def test_remove_relation(sample_users: list[User]) -> None:
    """Test remove_relation() removes the edge."""
    alice, bob = sample_users[0], sample_users[1]

    # Create then remove relation
    await alice.relate("follows", bob)
    await alice.remove_relation("follows", bob)

    # Verify relation removed
    following = await alice.get_related("follows", direction="out", model_class=User)
    assert len(following) == 0


@pytest.mark.integration
async def test_remove_relation_in_transaction(sample_users: list[User]) -> None:
    """Test remove_relation() within a transaction."""
    alice, bob, charlie = sample_users[0], sample_users[1], sample_users[2]

    # Create relations
    await alice.relate("follows", bob)
    await alice.relate("follows", charlie)

    # Remove in transaction
    async with await SurrealDBConnectionManager.transaction() as tx:
        await alice.remove_relation("follows", bob, tx=tx)

    # Verify only charlie remains
    following = await alice.get_related("follows", direction="out", model_class=User)
    assert len(following) == 1
    assert following[0].id == "charlie"


# ==================== Mixed Relations Tests ====================


@pytest.mark.integration
async def test_user_follows_user_and_likes_post(
    sample_users: list[User],
    sample_posts: list[Post],
) -> None:
    """Test multiple relation types."""
    alice, bob = sample_users[0], sample_users[1]
    post1, post2 = sample_posts[0], sample_posts[1]

    # Alice follows Bob
    await alice.relate("follows", bob)

    # Alice likes posts
    await alice.relate("likes", post1)
    await alice.relate("likes", post2)

    # Verify
    following = await alice.get_related("follows", direction="out", model_class=User)
    liked_posts = await alice.get_related("likes", direction="out", model_class=Post)

    assert len(following) == 1
    assert following[0].id == "bob"
    assert len(liked_posts) == 2


@pytest.mark.integration
async def test_bidirectional_relations(sample_users: list[User]) -> None:
    """Test mutual follow relationships."""
    alice, bob = sample_users[0], sample_users[1]

    # Mutual follow
    await alice.relate("follows", bob)
    await bob.relate("follows", alice)

    # Check both directions
    alice_following = await alice.get_related("follows", direction="out", model_class=User)
    alice_followers = await alice.get_related("follows", direction="in", model_class=User)

    assert len(alice_following) == 1
    assert alice_following[0].id == "bob"
    assert len(alice_followers) == 1
    assert alice_followers[0].id == "bob"


# ==================== QuerySet Traversal Tests ====================


@pytest.mark.integration
async def test_graph_query_basic(sample_users: list[User]) -> None:
    """Test graph_query() method."""
    alice, bob, charlie = sample_users[0], sample_users[1], sample_users[2]

    # Create relations
    await alice.relate("follows", bob)
    await alice.relate("follows", charlie)

    # Query using graph_query
    result = await User.objects().filter(id="alice").graph_query("->follows->User")

    assert len(result) == 2


@pytest.mark.integration
async def test_traverse_simple(sample_users: list[User]) -> None:
    """Test traverse() method."""
    alice, bob = sample_users[0], sample_users[1]

    await alice.relate("follows", bob)

    # Use traverse
    qs = User.objects().filter(id="alice").traverse("->follows->User")

    # Verify the path is stored
    assert qs._traversal_path == "->follows->User"


# ==================== Edge Cases ====================


@pytest.mark.integration
async def test_relate_same_user_twice(sample_users: list[User]) -> None:
    """Test creating the same relation twice."""
    alice, bob = sample_users[0], sample_users[1]

    # Create same relation twice
    await alice.relate("follows", bob)
    await alice.relate("follows", bob)  # Should create another edge or be idempotent

    # Check results
    following = await alice.get_related("follows", direction="out", model_class=User)
    # SurrealDB may create duplicate edges
    assert len(following) >= 1


@pytest.mark.integration
async def test_self_relation(sample_users: list[User]) -> None:
    """Test user can follow themselves."""
    alice = sample_users[0]

    # Self-follow
    await alice.relate("follows", alice)

    following = await alice.get_related("follows", direction="out", model_class=User)
    assert len(following) == 1
    assert following[0].id == "alice"
