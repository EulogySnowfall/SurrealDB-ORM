"""Tests for full-text search — v0.12.0."""

from typing import AsyncGenerator

import pytest

from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.search import SearchHighlight, SearchScore


# ── Test models ──────────────────────────────────────────────────────────────


class Post(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="posts")
    id: str | None = None
    title: str = ""
    body: str = ""
    published: bool = True


# ==================== Unit Tests ====================


class TestSearchScore:
    """Test SearchScore helper."""

    def test_to_surql(self) -> None:
        ss = SearchScore(0)
        assert ss.to_surql("relevance") == "search::score(0) AS relevance"

    def test_to_surql_ref1(self) -> None:
        ss = SearchScore(1)
        assert ss.to_surql("body_score") == "search::score(1) AS body_score"

    def test_repr(self) -> None:
        ss = SearchScore(2)
        assert repr(ss) == "SearchScore(2)"

    def test_eq(self) -> None:
        assert SearchScore(0) == SearchScore(0)
        assert SearchScore(0) != SearchScore(1)
        assert SearchScore(0) != "not a score"

    def test_hash(self) -> None:
        assert isinstance(hash(SearchScore(0)), int)


class TestSearchHighlight:
    """Test SearchHighlight helper."""

    def test_to_surql(self) -> None:
        sh = SearchHighlight("<b>", "</b>", 0)
        assert sh.to_surql("snippet") == "search::highlight('<b>', '</b>', 0) AS snippet"

    def test_to_surql_custom_tags(self) -> None:
        sh = SearchHighlight("<mark>", "</mark>", 1)
        assert sh.to_surql("hl") == "search::highlight('<mark>', '</mark>', 1) AS hl"

    def test_repr(self) -> None:
        sh = SearchHighlight("<b>", "</b>", 0)
        assert "SearchHighlight" in repr(sh)

    def test_eq(self) -> None:
        a = SearchHighlight("<b>", "</b>", 0)
        b = SearchHighlight("<b>", "</b>", 0)
        c = SearchHighlight("<em>", "</em>", 0)
        assert a == b
        assert a != c
        assert a != "not a highlight"

    def test_hash(self) -> None:
        assert isinstance(hash(SearchHighlight("<b>", "</b>", 0)), int)


class TestSearchMethod:
    """Test QuerySet.search() compilation."""

    def test_single_field_search(self) -> None:
        qs = Post.objects().search(title="quantum")
        query = qs._compile_query()
        assert "title @0@ $_s0" in query
        assert qs._variables["_s0"] == "quantum"

    def test_multi_field_search(self) -> None:
        qs = Post.objects().search(title="quantum", body="physics")
        query = qs._compile_query()
        assert "title @0@ $_s0" in query
        assert "body @1@ $_s1" in query
        assert " AND " in query
        assert qs._variables["_s0"] == "quantum"
        assert qs._variables["_s1"] == "physics"

    def test_search_with_filter(self) -> None:
        qs = Post.objects().filter(published=True).search(title="quantum")
        query = qs._compile_query()
        assert "published = $_f0" in query
        assert "title @0@ $_s0" in query
        assert " AND " in query

    def test_search_with_score_annotate(self) -> None:
        qs = (
            Post.objects()
            .search(title="quantum")
            .annotate(
                relevance=SearchScore(0),
            )
        )
        query = qs._compile_query()
        assert "search::score(0) AS relevance" in query
        assert "title @0@ $_s0" in query

    def test_search_with_highlight_annotate(self) -> None:
        qs = (
            Post.objects()
            .search(title="quantum")
            .annotate(
                snippet=SearchHighlight("<b>", "</b>", 0),
            )
        )
        query = qs._compile_query()
        assert "search::highlight('<b>', '</b>', 0) AS snippet" in query

    def test_search_combined_annotations(self) -> None:
        qs = (
            Post.objects()
            .search(title="quantum")
            .annotate(
                score=SearchScore(0),
                hl=SearchHighlight("<em>", "</em>", 0),
            )
        )
        query = qs._compile_query()
        assert "search::score(0) AS score" in query
        assert "search::highlight('<em>', '</em>', 0) AS hl" in query
        assert "title @0@ $_s0" in query


class TestSearchExport:
    """Test that search helpers are properly exported."""

    def test_import_from_surreal_orm(self) -> None:
        from src.surreal_orm import SearchHighlight as SHImport
        from src.surreal_orm import SearchScore as SSImport

        assert SSImport is SearchScore
        assert SHImport is SearchHighlight

    def test_in_all(self) -> None:
        import src.surreal_orm as orm

        assert "SearchScore" in orm.__all__
        assert "SearchHighlight" in orm.__all__


# ==================== Integration Tests ====================


@pytest.fixture(scope="module", autouse=True)
async def _setup_connection() -> AsyncGenerator[None, None]:
    """Set up ORM connection for integration tests."""
    from src.surreal_orm import SurrealDBConnectionManager

    SurrealDBConnectionManager.set_connection(
        "http://localhost:8000",
        "root",
        "root",
        "test",
        "test_search",
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


@pytest.mark.integration
class TestSearchIntegration:
    """Integration tests requiring a live SurrealDB instance."""

    @pytest.fixture(autouse=True)
    async def setup_data(self) -> None:
        """Create test data with FTS index."""
        from src.surreal_orm import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()

        # Clean up — use REMOVE IF EXISTS for idempotent re-runs
        await client.query("REMOVE INDEX IF EXISTS ft_title ON posts;")
        await client.query("REMOVE ANALYZER IF EXISTS post_analyzer;")
        await client.query("DELETE FROM posts;")

        # Define analyzer and search index
        await client.query("DEFINE ANALYZER post_analyzer TOKENIZERS blank, class FILTERS lowercase, snowball(english);")
        await client.query("DEFINE INDEX ft_title ON posts FIELDS title SEARCH ANALYZER post_analyzer BM25 HIGHLIGHTS;")

        # Create posts
        await client.query(
            "CREATE posts:p1 SET title = 'Quantum Physics Explained', body = 'An introduction to quantum mechanics.', published = true;"
        )
        await client.query("CREATE posts:p2 SET title = 'Classical Physics', body = 'Newton and gravity.', published = true;")
        await client.query("CREATE posts:p3 SET title = 'Cooking Recipes', body = 'How to make pasta.', published = true;")

    async def test_fts_search(self) -> None:
        """Basic full-text search returns matching posts."""
        posts = await Post.objects().search(title="physics").exec()
        assert len(posts) >= 1
        titles = [p.title for p in posts if hasattr(p, "title")]
        assert any("Physics" in t for t in titles)

    async def test_fts_scoring(self) -> None:
        """BM25 scoring returns ordered results."""
        posts = (
            await Post.objects()
            .search(title="quantum physics")
            .annotate(
                score=SearchScore(0),
            )
            .exec()
        )
        # Should return results with score attribute
        assert len(posts) >= 1
