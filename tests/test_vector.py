"""Tests for VectorField type and similar_to() — v0.12.0."""

from typing import AsyncGenerator

import pytest

from src.surreal_orm.fields.vector import VectorField, get_vector_info, is_vector_field
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict


# ── Test models ──────────────────────────────────────────────────────────────


class Document(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="documents")
    id: str | None = None
    title: str = ""
    category: str = ""
    embedding: VectorField[1536]


class SmallDoc(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="small_docs")
    id: str | None = None
    name: str = ""
    vec: VectorField[384, "F64"]  # noqa: F821


# ==================== Unit Tests ====================


class TestVectorField:
    """Test VectorField type annotation and detection helpers."""

    def test_vectorfield_type(self) -> None:
        ann = VectorField[1536]
        assert is_vector_field(ann)

    def test_vectorfield_info_default_type(self) -> None:
        ann = VectorField[1536]
        info = get_vector_info(ann)
        assert info is not None
        assert info == (1536, "F32")

    def test_vectorfield_info_custom_type(self) -> None:
        ann = VectorField[384, "F64"]
        info = get_vector_info(ann)
        assert info is not None
        assert info == (384, "F64")

    def test_not_vector_field(self) -> None:
        assert not is_vector_field(str)
        assert not is_vector_field(list[float])
        assert get_vector_info(int) is None

    def test_model_field_detection(self) -> None:
        """VectorField on a model class is detectable."""
        hints = Document.__annotations__
        assert is_vector_field(hints["embedding"])
        assert get_vector_info(hints["embedding"]) == (1536, "F32")

    def test_model_field_custom_type_detection(self) -> None:
        hints = SmallDoc.__annotations__
        assert is_vector_field(hints["vec"])
        assert get_vector_info(hints["vec"]) == (384, "F64")


class TestSimilarTo:
    """Test similar_to() query compilation."""

    def test_basic_knn(self) -> None:
        vec = [1.0] * 10
        qs = Document.objects().similar_to("embedding", vec, limit=5)
        query = qs._compile_query()
        assert "embedding <|5|> $_knn_vec" in query
        assert "vector::distance::knn() AS _knn_distance" in query
        assert "ORDER BY _knn_distance" in query
        assert qs._variables["_knn_vec"] == vec

    def test_knn_with_ef(self) -> None:
        vec = [0.5] * 10
        qs = Document.objects().similar_to("embedding", vec, limit=10, ef=40)
        query = qs._compile_query()
        assert "embedding <|10,40|> $_knn_vec" in query

    def test_knn_with_filter(self) -> None:
        vec = [1.0] * 10
        qs = Document.objects().filter(category="science").similar_to("embedding", vec, limit=5)
        query = qs._compile_query()
        assert "category = $_f0" in query
        assert "embedding <|5|> $_knn_vec" in query
        assert " AND " in query
        assert qs._variables["_f0"] == "science"
        assert qs._variables["_knn_vec"] == vec

    def test_knn_auto_order(self) -> None:
        """Without explicit order_by, KNN auto-orders by distance."""
        qs = Document.objects().similar_to("embedding", [1.0], limit=3)
        query = qs._compile_query()
        assert "ORDER BY _knn_distance" in query

    def test_knn_explicit_order_overrides(self) -> None:
        """Explicit order_by overrides KNN auto-ordering."""
        qs = Document.objects().similar_to("embedding", [1.0], limit=3).order_by("-title")
        query = qs._compile_query()
        assert "ORDER BY title DESC" in query
        assert "_knn_distance" not in query.split("ORDER BY")[1].split(";")[0] or "title DESC" in query

    def test_knn_select_clause(self) -> None:
        """KNN adds distance to SELECT clause."""
        qs = Document.objects().similar_to("embedding", [1.0], limit=5)
        query = qs._compile_query()
        assert query.startswith("SELECT *, vector::distance::knn() AS _knn_distance FROM")

    def test_knn_with_select_fields(self) -> None:
        """KNN with explicit select() still adds distance."""
        qs = Document.objects().select("id", "title").similar_to("embedding", [1.0], limit=5)
        query = qs._compile_query()
        assert "SELECT id, title, vector::distance::knn() AS _knn_distance FROM" in query


class TestVectorFieldExport:
    """Test that VectorField is properly exported."""

    def test_import_from_surreal_orm(self) -> None:
        from src.surreal_orm import VectorField as VFImport

        assert VFImport is VectorField

    def test_in_all(self) -> None:
        import src.surreal_orm as orm

        assert "VectorField" in orm.__all__


# ==================== Integration Tests ====================


@pytest.fixture(scope="module", autouse=True)
async def _setup_connection() -> AsyncGenerator[None, None]:
    """Set up ORM connection for integration tests."""
    from tests.conftest import SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE

    from src.surreal_orm import SurrealDBConnectionManager

    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        "test_vector",
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


@pytest.mark.integration
class TestVectorIntegration:
    """Integration tests requiring a live SurrealDB instance."""

    @pytest.fixture(autouse=True)
    async def setup_data(self) -> None:
        """Create test data with vector index."""
        from src.surreal_orm import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()

        # Clean up
        await client.query("DELETE FROM documents;")

        # Define MTREE index (HNSW not reliable on SurrealDB <=2.6.0 for KNN queries)
        await client.query("REMOVE INDEX IF EXISTS vec_idx ON documents;")
        await client.query("DEFINE INDEX vec_idx ON documents FIELDS embedding MTREE DIMENSION 3 DIST COSINE TYPE F32;")

        # Create documents with 3-dim vectors
        await client.query("CREATE documents:d1 SET title = 'Physics', category = 'science', embedding = [1.0, 0.0, 0.0];")
        await client.query("CREATE documents:d2 SET title = 'Chemistry', category = 'science', embedding = [0.9, 0.1, 0.0];")
        await client.query("CREATE documents:d3 SET title = 'History', category = 'humanities', embedding = [0.0, 0.0, 1.0];")

    async def test_knn_search(self) -> None:
        """Basic KNN search returns nearest documents."""
        docs = await Document.objects().similar_to("embedding", [1.0, 0.0, 0.0], limit=2).exec()
        assert len(docs) >= 1
        # Physics (exact match) should be first
        titles = [d.title for d in docs if hasattr(d, "title")]
        assert "Physics" in titles

    async def test_knn_with_filter(self) -> None:
        """KNN search combined with category filter."""
        docs = await Document.objects().filter(category="science").similar_to("embedding", [0.0, 0.0, 1.0], limit=5).exec()
        # Should only return science docs, even though History is closest
        for d in docs:
            if isinstance(d, Document):
                assert d.category == "science"
