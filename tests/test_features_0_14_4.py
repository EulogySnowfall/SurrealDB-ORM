"""
Integration tests for v0.14.4 features:

1. Datetime serialization fix — datetime objects survive round-trips through
   both model save() and raw_query(inline_dicts=True) paths.
2. Generic QuerySet[T] — terminal methods return properly typed instances.
3. get_related() @overload — typed return based on model_class parameter.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest

from src import surreal_orm
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_features_0_14_4"


# ── Models ─────────────────────────────────────────────────────────────


class DatetimeModel(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="dt_model")

    id: str | None = None
    name: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SimpleModel(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="simple_model")

    id: str | None = None
    name: str = ""
    age: int = 0


class RelAuthor(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rel_author")

    id: str | None = None
    name: str = ""


class RelBook(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="rel_book")

    id: str | None = None
    title: str = ""


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
async def setup_and_clean() -> AsyncGenerator[None, Any]:
    """Initialize connection and clean test tables."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    tables = ["dt_model", "simple_model", "rel_author", "rel_book", "wrote"]
    for table in tables:
        try:
            await client.query(f"REMOVE TABLE IF EXISTS {table};")
        except Exception:
            pass

    yield

    try:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        for table in tables:
            try:
                await client.query(f"REMOVE TABLE IF EXISTS {table};")
            except Exception:
                pass
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════════════════
# Feature 1: Datetime serialization round-trip
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_datetime_save_roundtrip() -> None:
    """datetime fields should survive save() -> get() round-trip as native datetimes."""
    now = datetime(2026, 2, 19, 10, 30, 45, tzinfo=UTC)
    m = DatetimeModel(id="dt_save_1", name="datetime test", created_at=now)
    await m.save()

    loaded = await DatetimeModel.objects().get("dt_save_1")
    assert isinstance(loaded.created_at, datetime)
    # Compare at second precision (SurrealDB may truncate sub-second)
    assert loaded.created_at.replace(microsecond=0) == now.replace(microsecond=0)


@pytest.mark.integration
async def test_datetime_update_roundtrip() -> None:
    """datetime fields should survive merge() -> get() round-trip."""
    now = datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)
    m = DatetimeModel(id="dt_update_1", name="update test", created_at=now)
    await m.save()

    new_time = datetime(2026, 4, 15, 14, 30, 0, tzinfo=UTC)
    await m.merge(updated_at=new_time)

    loaded = await DatetimeModel.objects().get("dt_update_1")
    assert isinstance(loaded.updated_at, datetime)
    assert loaded.updated_at.replace(microsecond=0) == new_time.replace(microsecond=0)


@pytest.mark.integration
async def test_datetime_inline_dict_roundtrip() -> None:
    """datetime inside inline_dicts=True should become d'...' literal, not plain string."""
    dt = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

    # Pass a datetime object inside a complex dict with inline_dicts=True
    # so that _extract_datetime_values() converts it to a d"..." SurrealQL literal.
    await DatetimeModel.raw_query(
        "UPSERT dt_model:dt_inline SET name = $data.name, created_at = $data.created_at;",
        variables={"data": {"name": "inline test", "created_at": dt}},
        inline_dicts=True,
    )

    loaded = await DatetimeModel.objects().get("dt_inline")
    assert loaded.name == "inline test"
    assert isinstance(loaded.created_at, datetime)
    assert loaded.created_at.replace(microsecond=0) == dt.replace(microsecond=0)


@pytest.mark.integration
async def test_datetime_none_roundtrip() -> None:
    """None datetime fields should round-trip correctly."""
    m = DatetimeModel(id="dt_none_1", name="none test", created_at=None)
    await m.save()

    loaded = await DatetimeModel.objects().get("dt_none_1")
    assert loaded.created_at is None


# ═════════════════════════════════════════════════════════════════════════
# Feature 2: Generic QuerySet[T] — integration tests
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_queryset_get_returns_model_instance() -> None:
    """QuerySet.get() should return an instance of the model class."""
    m = SimpleModel(id="qs_get_1", name="Alice", age=30)
    await m.save()

    result = await SimpleModel.objects().get("qs_get_1")
    assert isinstance(result, SimpleModel)
    assert result.name == "Alice"
    assert result.age == 30


@pytest.mark.integration
async def test_queryset_exec_returns_list_of_model() -> None:
    """QuerySet.exec() should return a list of model instances."""
    await SimpleModel(id="qs_exec_1", name="Bob", age=25).save()
    await SimpleModel(id="qs_exec_2", name="Carol", age=35).save()

    results = await SimpleModel.objects().filter(age__gte=20).exec()
    assert isinstance(results, list)
    assert len(results) >= 2
    for item in results:
        assert isinstance(item, SimpleModel)


@pytest.mark.integration
async def test_queryset_first_returns_model_instance() -> None:
    """QuerySet.first() should return a single model instance."""
    await SimpleModel(id="qs_first_1", name="Dave", age=40).save()

    result = await SimpleModel.objects().filter(name="Dave").first()
    assert isinstance(result, SimpleModel)
    assert result.name == "Dave"


@pytest.mark.integration
async def test_queryset_all_returns_list_of_model() -> None:
    """QuerySet.all() should return a list of model instances."""
    results = await SimpleModel.objects().all()
    assert isinstance(results, list)
    for item in results:
        assert isinstance(item, SimpleModel)


# ═════════════════════════════════════════════════════════════════════════
# Feature 3: get_related() @overload — integration tests
# ═════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_get_related_with_model_class() -> None:
    """get_related(model_class=X) should return list[X]."""
    author = RelAuthor(id="author_1", name="Author One")
    await author.save()

    book = RelBook(id="book_1", title="Book One")
    await book.save()

    await author.relate("wrote", book)

    related = await author.get_related("wrote", direction="out", model_class=RelBook)
    assert isinstance(related, list)
    assert len(related) >= 1
    for item in related:
        assert isinstance(item, RelBook)
    assert any(b.title == "Book One" for b in related)


@pytest.mark.integration
async def test_get_related_without_model_class() -> None:
    """get_related(model_class=None) should return list[dict]."""
    author = RelAuthor(id="author_2", name="Author Two")
    await author.save()

    book = RelBook(id="book_2", title="Book Two")
    await book.save()

    await author.relate("wrote", book)

    related = await author.get_related("wrote", direction="out")
    assert isinstance(related, list)
    assert len(related) >= 1
    for item in related:
        assert isinstance(item, dict)
