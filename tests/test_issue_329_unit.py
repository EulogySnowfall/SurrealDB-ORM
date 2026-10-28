"""Unit tests for #329: pagination and KNN arguments accept only integers.

``limit()``, ``offset()``, ``similar_to(limit, ef)`` and
``hybrid_search(vector_limit, ef, text_limit, rrf_k)`` write their argument into
the query text.  Anything other than a plain ``int`` (``bool`` excluded) raises
``TypeError`` at the call, and a value below the minimum raises ``ValueError``,
before any query is built.  Valid integers render exactly as before.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.subquery import Subquery


class U329Doc(BaseSurrealModel):
    """A table with a vector and a text field."""

    model_config = SurrealConfigDict(table_name="u329_docs")

    id: str | None = None
    title: str = ""
    embedding: list[float] = []


WRONG_TYPES: list[Any] = ["10", "1; SELECT * FROM u329_other", 2.0, 1.5, True, False, None, [1]]
WRONG_TYPE_IDS = ["str", "str-trailing-text", "float-integral", "float", "bool-true", "bool-false", "none", "list"]


class _FormattedInt(int):
    """An ``int`` subclass whose text differs from its value."""

    def __str__(self) -> str:
        return "7 extra"

    def __format__(self, spec: str) -> str:
        return "7 extra"

    __repr__ = __str__


# ── limit() / offset() ───────────────────────────────────────────────────────


@pytest.mark.parametrize("value", WRONG_TYPES, ids=WRONG_TYPE_IDS)
@pytest.mark.parametrize("method", ["limit", "offset"])
def test_pagination_rejects_non_int(method: str, value: Any) -> None:
    qs = U329Doc.objects()
    with pytest.raises(TypeError, match=method):
        getattr(qs, method)(value)


@pytest.mark.parametrize("method", ["limit", "offset"])
def test_pagination_rejects_negative(method: str) -> None:
    with pytest.raises(ValueError, match=method):
        getattr(U329Doc.objects(), method)(-1)


@pytest.mark.parametrize("method", ["limit", "offset"])
def test_pagination_accepts_zero(method: str) -> None:
    qs = getattr(U329Doc.objects(), method)(0)
    keyword = "LIMIT" if method == "limit" else "START"
    assert f" {keyword} 0" in qs._compile_query()


def test_pagination_rejected_value_leaves_queryset_unchanged() -> None:
    qs = U329Doc.objects().limit(5).offset(2)
    with pytest.raises(TypeError):
        qs.limit("3")
    with pytest.raises(TypeError):
        qs.offset("3")
    assert qs._compile_query() == "SELECT * FROM u329_docs LIMIT 5 START 2;"


def test_pagination_valid_integers_render_as_before() -> None:
    query = U329Doc.objects().limit(10).offset(20)._compile_query()
    assert query == "SELECT * FROM u329_docs LIMIT 10 START 20;"


def test_pagination_int_subclass_renders_its_value() -> None:
    query = U329Doc.objects().limit(_FormattedInt(7)).offset(_FormattedInt(7))._compile_query()
    assert query == "SELECT * FROM u329_docs LIMIT 7 START 7;"


def test_subquery_uses_validated_pagination() -> None:
    inner = U329Doc.objects().select("id").limit(3).offset(1)
    prelude: list[str] = []
    rendered = Subquery(inner).to_surql({}, [0], prelude)
    text = rendered + " ".join(prelude)
    assert "LIMIT 3" in text
    assert "START 1" in text


# ── similar_to() ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", WRONG_TYPES, ids=WRONG_TYPE_IDS)
def test_similar_to_limit_rejects_non_int(value: Any) -> None:
    with pytest.raises(TypeError, match="limit"):
        U329Doc.objects().similar_to("embedding", [1.0], limit=value)


@pytest.mark.parametrize("value", [0, -1])
def test_similar_to_limit_must_be_positive(value: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        U329Doc.objects().similar_to("embedding", [1.0], limit=value)


@pytest.mark.parametrize("value", [v for v in WRONG_TYPES if v is not None], ids=[i for i in WRONG_TYPE_IDS if i != "none"])
def test_similar_to_ef_rejects_non_int(value: Any) -> None:
    with pytest.raises(TypeError, match="ef"):
        U329Doc.objects().similar_to("embedding", [1.0], limit=5, ef=value)


def test_similar_to_ef_rejects_negative() -> None:
    with pytest.raises(ValueError, match="ef"):
        U329Doc.objects().similar_to("embedding", [1.0], limit=5, ef=-1)


def test_similar_to_valid_integers_render_as_before() -> None:
    assert "embedding <|5,100|> $_knn_vec" in U329Doc.objects().similar_to("embedding", [1.0], limit=5)._compile_query()
    assert "embedding <|10,40|> $_knn_vec" in U329Doc.objects().similar_to("embedding", [1.0], limit=10, ef=40)._compile_query()
    assert "embedding <|5,0|> $_knn_vec" in U329Doc.objects().similar_to("embedding", [1.0], limit=5, ef=0)._compile_query()


def test_similar_to_int_subclass_renders_its_value() -> None:
    query = U329Doc.objects().similar_to("embedding", [1.0], limit=_FormattedInt(7), ef=_FormattedInt(7))._compile_query()
    assert "embedding <|7,7|> $_knn_vec" in query


# ── hybrid_search() ──────────────────────────────────────────────────────────

HYBRID_ARGS = ["vector_limit", "ef", "text_limit", "rrf_k"]
HYBRID_MINIMUM = {"vector_limit": 1, "ef": 0, "text_limit": 1, "rrf_k": 1}


def _hybrid_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "vector_field": "embedding",
        "vector": [1.0, 0.0],
        "text_field": "title",
        "text_query": "quantum",
    }
    kwargs.update(overrides)
    return kwargs


def _mock_client() -> MagicMock:
    client = MagicMock()
    result = MagicMock()
    result.all_records = []
    client.query = AsyncMock(return_value=result)
    return client


@pytest.mark.parametrize("value", WRONG_TYPES, ids=WRONG_TYPE_IDS)
@pytest.mark.parametrize("arg", HYBRID_ARGS)
async def test_hybrid_search_rejects_non_int(arg: str, value: Any) -> None:
    client = _mock_client()
    with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
        with pytest.raises(TypeError, match=arg):
            await U329Doc.objects().hybrid_search(**_hybrid_kwargs(**{arg: value}))
    client.query.assert_not_called()


@pytest.mark.parametrize("arg", HYBRID_ARGS)
async def test_hybrid_search_rejects_below_minimum(arg: str) -> None:
    client = _mock_client()
    with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
        with pytest.raises(ValueError, match=arg):
            await U329Doc.objects().hybrid_search(**_hybrid_kwargs(**{arg: HYBRID_MINIMUM[arg] - 1}))
    client.query.assert_not_called()


async def test_hybrid_search_valid_integers_render_as_before() -> None:
    client = _mock_client()
    with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
        await U329Doc.objects().hybrid_search(**_hybrid_kwargs(vector_limit=10, text_limit=15, rrf_k=60, ef=40))
    query = client.query.call_args.args[0]
    assert query == (
        "LET $vec_results = (SELECT id, vector::distance::knn() AS _d "
        "FROM u329_docs WHERE embedding <|10,40|> $_hybrid_vec "
        "ORDER BY _d);\n"
        "LET $fts_results = (SELECT id, search::score(0) AS _s "
        "FROM u329_docs WHERE title @0@ $_hybrid_text "
        "ORDER BY _s DESC LIMIT 15);\n"
        "SELECT *, "
        "search::rrf(60, $vec_results, $fts_results) AS _rrf_score "
        "FROM array::union($vec_results.id, $fts_results.id) "
        "ORDER BY _rrf_score DESC;"
    )
