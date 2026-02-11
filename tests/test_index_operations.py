"""Tests for index operations and parser — v0.12.0."""

import pytest

from src.surreal_orm.migrations.define_parser import parse_define_analyzer, parse_define_index
from src.surreal_orm.migrations.operations import CreateIndex, DefineAnalyzer, RemoveAnalyzer
from src.surreal_orm.migrations.state import AnalyzerState, IndexState, SchemaState, TableState


# ==================== CreateIndex Tests ====================


class TestCreateIndexHNSW:
    """Test CreateIndex forwards() for HNSW vector indexes."""

    def test_basic_hnsw(self) -> None:
        ci = CreateIndex(
            table="documents",
            name="vec_idx",
            fields=["embedding"],
            hnsw=True,
            dimension=1536,
            dist="COSINE",
        )
        sql = ci.forwards()
        assert sql == "DEFINE INDEX vec_idx ON documents FIELDS embedding HNSW DIMENSION 1536 DIST COSINE;"

    def test_hnsw_with_type_and_params(self) -> None:
        ci = CreateIndex(
            table="docs",
            name="vec_idx",
            fields=["embedding"],
            hnsw=True,
            dimension=384,
            dist="EUCLIDEAN",
            vector_type="F64",
            efc=150,
            m=12,
        )
        sql = ci.forwards()
        assert "HNSW" in sql
        assert "DIMENSION 384" in sql
        assert "DIST EUCLIDEAN" in sql
        assert "TYPE F64" in sql
        assert "EFC 150" in sql
        assert "M 12" in sql

    def test_hnsw_concurrently(self) -> None:
        ci = CreateIndex(
            table="docs",
            name="vec_idx",
            fields=["embedding"],
            hnsw=True,
            dimension=768,
            dist="COSINE",
            concurrently=True,
        )
        sql = ci.forwards()
        assert "CONCURRENTLY" in sql

    def test_backwards_removes_index(self) -> None:
        ci = CreateIndex(
            table="docs",
            name="vec_idx",
            fields=["embedding"],
            hnsw=True,
            dimension=1536,
        )
        assert ci.backwards() == "REMOVE INDEX vec_idx ON docs;"


class TestCreateIndexFTS:
    """Test CreateIndex forwards() for FTS indexes."""

    def test_fts_bm25_default(self) -> None:
        ci = CreateIndex(
            table="posts",
            name="ft_title",
            fields=["title"],
            search_analyzer="my_az",
            bm25=True,
        )
        sql = ci.forwards()
        assert "SEARCH ANALYZER my_az" in sql
        assert "BM25" in sql
        # Should not have parenthesized params
        assert "BM25(" not in sql

    def test_fts_bm25_custom_params(self) -> None:
        ci = CreateIndex(
            table="posts",
            name="ft_title",
            fields=["title"],
            search_analyzer="my_az",
            bm25=(1.2, 0.75),
        )
        sql = ci.forwards()
        assert "BM25(1.2,0.75)" in sql

    def test_fts_highlights(self) -> None:
        ci = CreateIndex(
            table="posts",
            name="ft_title",
            fields=["title"],
            search_analyzer="my_az",
            bm25=True,
            highlights=True,
        )
        sql = ci.forwards()
        assert "HIGHLIGHTS" in sql

    def test_fts_full(self) -> None:
        ci = CreateIndex(
            table="posts",
            name="ft_title",
            fields=["title"],
            search_analyzer="my_az",
            bm25=(1.2, 0.75),
            highlights=True,
        )
        sql = ci.forwards()
        assert sql == ("DEFINE INDEX ft_title ON posts FIELDS title SEARCH ANALYZER my_az BM25(1.2,0.75) HIGHLIGHTS;")


class TestCreateIndexStandard:
    """Test CreateIndex for standard and unique indexes (regression)."""

    def test_unique_index(self) -> None:
        ci = CreateIndex(table="users", name="email_idx", fields=["email"], unique=True)
        assert ci.forwards() == "DEFINE INDEX email_idx ON users FIELDS email UNIQUE;"

    def test_simple_index(self) -> None:
        ci = CreateIndex(table="users", name="name_idx", fields=["name"])
        assert ci.forwards() == "DEFINE INDEX name_idx ON users FIELDS name;"

    def test_multi_field_index(self) -> None:
        ci = CreateIndex(table="orders", name="user_status_idx", fields=["user_id", "status"])
        assert ci.forwards() == "DEFINE INDEX user_status_idx ON orders FIELDS user_id, status;"


# ==================== DefineAnalyzer Tests ====================


class TestDefineAnalyzer:
    """Test DefineAnalyzer operation."""

    def test_forwards(self) -> None:
        da = DefineAnalyzer(
            name="my_analyzer",
            tokenizers=["blank", "class"],
            filters=["lowercase", "snowball(english)"],
        )
        sql = da.forwards()
        assert sql == "DEFINE ANALYZER my_analyzer TOKENIZERS blank, class FILTERS lowercase, snowball(english);"

    def test_backwards(self) -> None:
        da = DefineAnalyzer(name="my_analyzer", tokenizers=["blank"], filters=["lowercase"])
        assert da.backwards() == "REMOVE ANALYZER my_analyzer;"

    def test_empty_filters(self) -> None:
        da = DefineAnalyzer(name="simple", tokenizers=["blank"])
        sql = da.forwards()
        assert sql == "DEFINE ANALYZER simple TOKENIZERS blank;"
        assert "FILTERS" not in sql

    def test_describe(self) -> None:
        da = DefineAnalyzer(name="my_az", tokenizers=[], filters=[])
        assert "my_az" in da.describe()


class TestRemoveAnalyzer:
    """Test RemoveAnalyzer operation."""

    def test_forwards(self) -> None:
        ra = RemoveAnalyzer(name="old_az")
        assert ra.forwards() == "REMOVE ANALYZER old_az;"

    def test_not_reversible(self) -> None:
        ra = RemoveAnalyzer(name="old_az")
        assert not ra.reversible


# ==================== Parser Tests ====================


class TestParseDefineIndexHNSW:
    """Test parse_define_index with HNSW statements."""

    def test_basic_hnsw(self) -> None:
        idx = parse_define_index("DEFINE INDEX vec_idx ON documents FIELDS embedding HNSW DIMENSION 1536 DIST COSINE TYPE F32")
        assert idx.name == "vec_idx"
        assert idx.fields == ["embedding"]
        assert idx.hnsw is True
        assert idx.dimension == 1536
        assert idx.dist == "COSINE"
        assert idx.vector_type == "F32"

    def test_hnsw_with_efc_m(self) -> None:
        idx = parse_define_index("DEFINE INDEX vec_idx ON docs FIELDS embedding HNSW DIMENSION 384 DIST EUCLIDEAN EFC 150 M 12")
        assert idx.efc == 150
        assert idx.m == 12
        assert idx.dimension == 384
        assert idx.dist == "EUCLIDEAN"

    def test_hnsw_concurrently(self) -> None:
        idx = parse_define_index("DEFINE INDEX vec_idx ON docs FIELDS embedding HNSW DIMENSION 768 CONCURRENTLY")
        assert idx.concurrently is True
        assert idx.dimension == 768


class TestParseDefineIndexFTS:
    """Test parse_define_index with FTS statements."""

    def test_bm25_default(self) -> None:
        idx = parse_define_index("DEFINE INDEX ft_title ON posts FIELDS title SEARCH ANALYZER my_az BM25")
        assert idx.search_analyzer == "my_az"
        assert idx.bm25 is True
        assert idx.highlights is False

    def test_bm25_params(self) -> None:
        idx = parse_define_index("DEFINE INDEX ft_title ON posts FIELDS title SEARCH ANALYZER my_az BM25(1.2, 0.75) HIGHLIGHTS")
        assert idx.bm25 == (1.2, 0.75)
        assert idx.highlights is True

    def test_highlights_only(self) -> None:
        idx = parse_define_index("DEFINE INDEX ft_title ON posts FIELDS title SEARCH ANALYZER my_az BM25 HIGHLIGHTS")
        assert idx.bm25 is True
        assert idx.highlights is True


class TestParseDefineIndexRegression:
    """Regression tests for existing parse_define_index behavior."""

    def test_unique_index(self) -> None:
        idx = parse_define_index("DEFINE INDEX email_idx ON users FIELDS email UNIQUE")
        assert idx.unique is True
        assert idx.hnsw is False
        assert idx.bm25 is None

    def test_search_analyzer_only(self) -> None:
        idx = parse_define_index("DEFINE INDEX ft_idx ON posts FIELDS title SEARCH ANALYZER my_az")
        assert idx.search_analyzer == "my_az"
        assert idx.bm25 is None


class TestParseDefineAnalyzer:
    """Test parse_define_analyzer."""

    def test_basic(self) -> None:
        result = parse_define_analyzer("DEFINE ANALYZER my_az TOKENIZERS blank, class FILTERS lowercase, snowball(english)")
        assert result["name"] == "my_az"
        assert result["tokenizers"] == ["blank", "class"]
        assert result["filters"] == ["lowercase", "snowball(english)"]

    def test_single_tokenizer(self) -> None:
        result = parse_define_analyzer("DEFINE ANALYZER simple TOKENIZERS blank FILTERS lowercase")
        assert result["tokenizers"] == ["blank"]
        assert result["filters"] == ["lowercase"]

    def test_multiple_filters_with_params(self) -> None:
        result = parse_define_analyzer(
            "DEFINE ANALYZER complex TOKENIZERS blank FILTERS lowercase, edgengram(2, 10), snowball(english)"
        )
        assert len(result["filters"]) == 3
        assert "edgengram(2, 10)" in result["filters"]

    def test_no_filters(self) -> None:
        result = parse_define_analyzer("DEFINE ANALYZER tok_only TOKENIZERS blank, punct")
        assert result["tokenizers"] == ["blank", "punct"]
        assert result["filters"] == []

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_define_analyzer("NOT A VALID STATEMENT")


# ==================== IndexState Diff Tests ====================


class TestIndexStateDiff:
    """Test schema diff detects index changes."""

    def test_new_hnsw_index_detected(self) -> None:
        current = SchemaState(
            tables={
                "docs": TableState(name="docs", fields={}, indexes={}),
            }
        )
        target = SchemaState(
            tables={
                "docs": TableState(
                    name="docs",
                    fields={},
                    indexes={
                        "vec_idx": IndexState(
                            name="vec_idx",
                            fields=["embedding"],
                            hnsw=True,
                            dimension=1536,
                            dist="COSINE",
                        ),
                    },
                ),
            }
        )
        ops = current.diff(target)
        create_ops = [o for o in ops if type(o).__name__ == "CreateIndex"]
        assert len(create_ops) == 1
        assert create_ops[0].hnsw is True
        assert create_ops[0].dimension == 1536

    def test_changed_hnsw_index_recreated(self) -> None:
        current = SchemaState(
            tables={
                "docs": TableState(
                    name="docs",
                    indexes={
                        "vec_idx": IndexState(name="vec_idx", fields=["embedding"], hnsw=True, dimension=768),
                    },
                ),
            }
        )
        target = SchemaState(
            tables={
                "docs": TableState(
                    name="docs",
                    indexes={
                        "vec_idx": IndexState(name="vec_idx", fields=["embedding"], hnsw=True, dimension=1536),
                    },
                ),
            }
        )
        ops = current.diff(target)
        # Should drop old + create new
        drop_ops = [o for o in ops if type(o).__name__ == "DropIndex"]
        create_ops = [o for o in ops if type(o).__name__ == "CreateIndex"]
        assert len(drop_ops) == 1
        assert len(create_ops) == 1
        assert create_ops[0].dimension == 1536

    def test_analyzer_diff(self) -> None:
        current = SchemaState(analyzers={})
        target = SchemaState(
            analyzers={
                "my_az": AnalyzerState(name="my_az", tokenizers=["blank"], filters=["lowercase"]),
            }
        )
        ops = current.diff(target)
        define_ops = [o for o in ops if type(o).__name__ == "DefineAnalyzer"]
        assert len(define_ops) == 1
        assert define_ops[0].name == "my_az"

    def test_analyzer_removed(self) -> None:
        current = SchemaState(
            analyzers={
                "old_az": AnalyzerState(name="old_az", tokenizers=["blank"], filters=[]),
            }
        )
        target = SchemaState(analyzers={})
        ops = current.diff(target)
        remove_ops = [o for o in ops if type(o).__name__ == "RemoveAnalyzer"]
        assert len(remove_ops) == 1
        assert remove_ops[0].name == "old_az"

    def test_analyzer_changed(self) -> None:
        current = SchemaState(
            analyzers={
                "my_az": AnalyzerState(name="my_az", tokenizers=["blank"], filters=["lowercase"]),
            }
        )
        target = SchemaState(
            analyzers={
                "my_az": AnalyzerState(name="my_az", tokenizers=["blank", "class"], filters=["lowercase"]),
            }
        )
        ops = current.diff(target)
        define_ops = [o for o in ops if type(o).__name__ == "DefineAnalyzer"]
        assert len(define_ops) == 1
        assert define_ops[0].tokenizers == ["blank", "class"]
