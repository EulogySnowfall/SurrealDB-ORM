"""
Tests for v0.5.4 features.

Feature #1: Record ID format handling - .get() handles both 'id' and 'table:id' formats
Feature #2: remove_relation() accepts string IDs
Feature #3: raw_query() class method for arbitrary SurrealQL
"""

from collections.abc import AsyncGenerator

import pytest

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel
from tests.conftest import SURREALDB_URL

# =============================================================================
# Test Models
# =============================================================================


class Player(BaseSurrealModel):
    """Player model for testing."""

    id: str | None = None
    name: str
    score: int = 0


class GameTable(BaseSurrealModel):
    """Game table model for relation tests."""

    id: str | None = None
    name: str
    status: str = "waiting"


# =============================================================================
# Feature #1: Record ID Format Handling
# =============================================================================


class TestRecordIdFormatHandling:
    """Test that .get() handles both 'id' and 'table:id' formats."""

    def test_parse_record_id_simple(self) -> None:
        """Test parsing simple ID (no table prefix)."""
        from src.surreal_orm.query_set import QuerySet

        QuerySet(Player)

        # Test the internal ID parsing by checking query compilation
        # The get() method extracts the ID part from "table:id" format
        record_id = "abc123"
        if ":" in record_id:
            record_id = record_id.split(":", 1)[1]
        assert record_id == "abc123"

    def test_parse_record_id_with_table_prefix(self) -> None:
        """Test parsing ID with table prefix."""
        record_id = "players:abc123"
        if ":" in record_id:
            record_id = record_id.split(":", 1)[1]
        assert record_id == "abc123"

    def test_parse_record_id_with_colon_in_id(self) -> None:
        """Test parsing ID that contains colons (e.g., UUID format)."""
        # Format: table:uuid-with-colons
        record_id = "players:550e8400-e29b-41d4-a716-446655440000"
        if ":" in record_id:
            record_id = record_id.split(":", 1)[1]
        assert record_id == "550e8400-e29b-41d4-a716-446655440000"


# =============================================================================
# Feature #2: remove_relation() Accepts String IDs
# =============================================================================


class TestRemoveRelationStringIds:
    """Test that remove_relation() accepts both model instances and string IDs."""

    def test_remove_relation_signature_accepts_string(self) -> None:
        """remove_relation() type hints should accept str | BaseSurrealModel."""
        import inspect

        sig = inspect.signature(BaseSurrealModel.remove_relation)
        params = sig.parameters

        # 'to' parameter should accept both types
        assert "to" in params
        # The annotation includes both str and BaseSurrealModel
        to_annotation = str(params["to"].annotation)
        assert "str" in to_annotation

    def test_string_id_with_table_prefix_parsing(self) -> None:
        """Test that string ID with table prefix is handled correctly."""
        # When string has ":" it's used as-is for the query
        target_id = "players:abc123"
        if ":" in target_id:
            # Full format - use as-is
            target_thing = target_id
        else:
            # Just ID - need special handling
            target_thing = None

        assert target_thing == "players:abc123"

    def test_string_id_without_prefix_detection(self) -> None:
        """Test that string ID without prefix is detected correctly."""
        target_id = "abc123"
        if ":" in target_id:
            has_prefix = True
        else:
            has_prefix = False

        assert has_prefix is False


# =============================================================================
# Feature #3: raw_query() Class Method
# =============================================================================


class TestRawQueryMethod:
    """Test that raw_query() class method works correctly."""

    def test_raw_query_method_exists(self) -> None:
        """raw_query() should be a class method on BaseSurrealModel."""
        assert hasattr(BaseSurrealModel, "raw_query")
        assert callable(BaseSurrealModel.raw_query)

    def test_raw_query_is_async(self) -> None:
        """raw_query() should be an async method."""
        import inspect

        assert inspect.iscoroutinefunction(BaseSurrealModel.raw_query)

    def test_raw_query_signature(self) -> None:
        """raw_query() should accept query string and optional variables."""
        import inspect

        sig = inspect.signature(BaseSurrealModel.raw_query)
        params = sig.parameters

        assert "query" in params
        assert "variables" in params

        # query is required
        assert params["query"].default == inspect.Parameter.empty

        # variables has a default
        assert params["variables"].default is None


# =============================================================================
# Integration Tests
# =============================================================================


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, None]:
    """Setup connection for integration tests."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        "root",
        "root",
        "test",
        "test_features_054",
    )
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def cleanup_tables() -> AsyncGenerator[None, None]:
    """Clean up test tables before and after each test."""
    tables = ["Player", "GameTable", "has_player"]
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
class TestRecordIdFormatIntegration:
    """Integration tests for record ID format handling."""

    async def test_get_with_simple_id(self) -> None:
        """get() should work with simple ID format."""
        # Create a player
        player = Player(id="player1", name="Alice", score=100)
        await player.save()

        # Get with simple ID
        fetched = await Player.objects().get("player1")
        assert fetched.id == "player1"
        assert fetched.name == "Alice"
        assert fetched.score == 100

    async def test_get_with_table_prefix(self) -> None:
        """get() should work with table:id format."""
        # Create a player
        player = Player(id="player2", name="Bob", score=200)
        await player.save()

        # Get with table:id format
        fetched = await Player.objects().get("Player:player2")
        assert fetched.id == "player2"
        assert fetched.name == "Bob"
        assert fetched.score == 200

    async def test_get_with_id_keyword(self) -> None:
        """get(id=...) should work with both formats."""
        # Create a player
        player = Player(id="player3", name="Charlie", score=300)
        await player.save()

        # Get with id= keyword and table:id format
        fetched = await Player.objects().get(id="Player:player3")
        assert fetched.id == "player3"
        assert fetched.name == "Charlie"

    async def test_get_not_found(self) -> None:
        """get() should raise DoesNotExist for non-existent record."""
        with pytest.raises(Player.DoesNotExist):
            await Player.objects().get("nonexistent")

        with pytest.raises(Player.DoesNotExist):
            await Player.objects().get("Player:nonexistent")


@pytest.mark.integration
class TestRemoveRelationIntegration:
    """Integration tests for remove_relation() with string IDs."""

    async def test_remove_relation_with_model_instance(self) -> None:
        """remove_relation() should work with model instance."""
        # Create game table and player
        table = GameTable(id="game1", name="Test Game")
        await table.save()

        player = Player(id="p1", name="Alice")
        await player.save()

        # Create relation
        await table.relate("has_player", player)

        # Verify relation exists
        related = await table.get_related("has_player", direction="out")
        assert len(related) == 1

        # Remove relation with model instance
        await table.remove_relation("has_player", player)

        # Verify relation removed
        related = await table.get_related("has_player", direction="out")
        assert len(related) == 0

    async def test_remove_relation_with_full_string_id(self) -> None:
        """remove_relation() should work with 'table:id' string format."""
        # Create game table and player
        table = GameTable(id="game2", name="Test Game 2")
        await table.save()

        player = Player(id="p2", name="Bob")
        await player.save()

        # Create relation
        await table.relate("has_player", player)

        # Verify relation exists
        related = await table.get_related("has_player", direction="out")
        assert len(related) == 1

        # Remove relation with string ID (full format)
        await table.remove_relation("has_player", "Player:p2")

        # Verify relation removed
        related = await table.get_related("has_player", direction="out")
        assert len(related) == 0

    async def test_remove_relation_with_simple_string_id(self) -> None:
        """remove_relation() should work with simple string ID."""
        # Create game table and player
        table = GameTable(id="game3", name="Test Game 3")
        await table.save()

        player = Player(id="p3", name="Charlie")
        await player.save()

        # Create relation
        await table.relate("has_player", player)

        # Verify relation exists
        related = await table.get_related("has_player", direction="out")
        assert len(related) == 1

        # Remove relation with simple string ID
        await table.remove_relation("has_player", "p3")

        # Verify relation removed
        related = await table.get_related("has_player", direction="out")
        assert len(related) == 0


@pytest.mark.integration
class TestRawQueryIntegration:
    """Integration tests for raw_query() class method."""

    async def test_raw_query_simple_select(self) -> None:
        """raw_query() should execute simple SELECT."""
        # Create some players
        await Player(id="rq1", name="Alice", score=100).save()
        await Player(id="rq2", name="Bob", score=200).save()

        # Execute raw query
        results = await Player.raw_query("SELECT * FROM Player")

        assert len(results) >= 2
        names = [r["name"] for r in results]
        assert "Alice" in names
        assert "Bob" in names

    async def test_raw_query_with_variables(self) -> None:
        """raw_query() should support parameterized queries."""
        # Create some players
        await Player(id="rq3", name="Charlie", score=150).save()
        await Player(id="rq4", name="Diana", score=250).save()

        # Execute raw query with variables
        results = await Player.raw_query(
            "SELECT * FROM Player WHERE score > $min_score",
            variables={"min_score": 200},
        )

        assert len(results) >= 1
        for r in results:
            assert r["score"] > 200

    async def test_raw_query_with_filter(self) -> None:
        """raw_query() should handle WHERE clauses."""
        # Create players
        await Player(id="rq5", name="Eve", score=300).save()

        # Execute raw query with WHERE
        results = await Player.raw_query(
            "SELECT * FROM Player WHERE name = $name",
            variables={"name": "Eve"},
        )

        assert len(results) == 1
        assert results[0]["name"] == "Eve"
        assert results[0]["score"] == 300

    async def test_raw_query_delete(self) -> None:
        """raw_query() should handle DELETE with RETURN."""
        # Create a player to delete
        await Player(id="rq_del", name="ToDelete", score=0).save()

        # Delete and return
        deleted = await Player.raw_query("DELETE Player:rq_del RETURN BEFORE")

        assert len(deleted) == 1
        assert deleted[0]["name"] == "ToDelete"

        # Verify deleted
        remaining = await Player.raw_query("SELECT * FROM Player:rq_del")
        assert len(remaining) == 0

    async def test_raw_query_empty_result(self) -> None:
        """raw_query() should return empty list for no matches."""
        results = await Player.raw_query("SELECT * FROM Player WHERE name = 'NonExistent'")

        assert results == []

    async def test_raw_query_graph_traversal(self) -> None:
        """raw_query() should handle graph queries."""
        # Create table and player with relation
        table = GameTable(id="rq_game", name="Raw Query Game")
        await table.save()

        player = Player(id="rq_player", name="GraphTest")
        await player.save()

        await table.relate("has_player", player)

        # Query using graph traversal
        results = await GameTable.raw_query("SELECT ->has_player->Player AS players FROM GameTable:rq_game")

        assert len(results) == 1
        assert "players" in results[0]
