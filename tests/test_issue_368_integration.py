"""Integration tests for #368: pagination and KNN arguments accept only integers (v2).

On the pinned 2.x server, a string passed as ``limit``/``offset``/KNN argument
is refused at the call and the database is left as it was, while integer
pagination and ``similar_to()`` keep returning the expected rows.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_368"

TRAILING = "1; UPDATE i368_witness SET v = 2"


class I368Doc(BaseSurrealModel):
    """Ordered records with a 3-dimension vector."""

    model_config = SurrealConfigDict(table_name="i368_docs")

    id: str | None = None
    rank: int = 0
    embedding: list[float] = []


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, Any]:
    """Point the ORM at a database of its own for this module."""
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
async def seed() -> AsyncGenerator[None, Any]:
    """Five ranked documents under a vector index, and one witness row."""
    await I368Doc.raw_query("REMOVE TABLE IF EXISTS i368_docs; REMOVE TABLE IF EXISTS i368_witness;")
    await I368Doc.raw_query("DEFINE INDEX i368_vec ON i368_docs FIELDS embedding MTREE DIMENSION 3 DIST COSINE TYPE F32;")
    vectors = [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 1.0, 0.0], [0.0, 0.1, 0.9], [0.0, 0.0, 1.0]]
    for rank, vec in enumerate(vectors):
        await I368Doc(id=f"d{rank}", rank=rank, embedding=vec).save()
    await I368Doc.raw_query("CREATE i368_witness:one SET v = 1;")
    yield
    await I368Doc.raw_query("REMOVE TABLE IF EXISTS i368_docs; REMOVE TABLE IF EXISTS i368_witness;")


async def _snapshot() -> tuple[Any, Any]:
    """Every row of both tables."""
    docs = await I368Doc.raw_query("SELECT id, rank FROM i368_docs ORDER BY id;")
    witness = await I368Doc.raw_query("SELECT * FROM i368_witness ORDER BY id;")
    return docs, witness


@pytest.mark.integration
async def test_string_arguments_are_refused_and_rows_stay_intact() -> None:
    """Every entry point refuses the string; no statement reaches the server."""
    before = await _snapshot()

    with pytest.raises(TypeError):
        await I368Doc.objects().limit(TRAILING).exec()  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await I368Doc.objects().offset(TRAILING).exec()  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await I368Doc.objects().similar_to("embedding", [1.0, 0.0, 0.0], limit=TRAILING).exec()  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await I368Doc.objects().similar_to("embedding", [1.0, 0.0, 0.0], limit=2, ef=TRAILING).exec()  # type: ignore[arg-type]
    for arg in ("vector_limit", "text_limit", "rrf_k"):
        with pytest.raises(TypeError, match=arg):
            await I368Doc.objects().hybrid_search(
                vector_field="embedding",
                vector=[1.0, 0.0, 0.0],
                text_field="rank",
                text_query="x",
                **{arg: TRAILING},
            )

    assert await _snapshot() == before
    witness = await I368Doc.raw_query("SELECT VALUE v FROM i368_witness;")
    assert witness == [1]


@pytest.mark.integration
async def test_limit_offset_pagination_returns_expected_rows() -> None:
    """Integer pagination pages through the ranked rows in order."""
    first = await I368Doc.objects().order_by("rank").limit(2).exec()
    second = await I368Doc.objects().order_by("rank").limit(2).offset(2).exec()
    last = await I368Doc.objects().order_by("rank").limit(2).offset(4).exec()
    assert [d.rank for d in first] == [0, 1]
    assert [d.rank for d in second] == [2, 3]
    assert [d.rank for d in last] == [4]


@pytest.mark.integration
async def test_similar_to_returns_nearest_rows() -> None:
    """KNN with an integer limit returns the two nearest documents, nearest first."""
    docs = await I368Doc.objects().similar_to("embedding", [1.0, 0.0, 0.0], limit=2).exec()
    assert [d.rank for d in docs] == [0, 1]
