"""Integration tests for ForeignKey fields against a real ``record<>`` column.

Every test here fails without the coercion: the write is rejected by SurrealDB,
and the filter returns an empty result for a row that demonstrably exists.
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import Field

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.fields.relation import ForeignKey
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.q import Q
from surreal_sdk.protocol.cbor import RecordId
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_fk_coercion"


class Author(BaseSurrealModel):
    """Target of the foreign key."""

    model_config = SurrealConfigDict(table_name="fk_authors")

    id: str | None = None
    name: str = Field(default="")


class Article(BaseSurrealModel):
    """Model whose author column is a real record<fk_authors>."""

    model_config = SurrealConfigDict(table_name="fk_articles")

    id: str | None = None
    title: str = Field(default="")
    author: ForeignKey("Author")  # type: ignore[valid-type]


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
async def schemafull_tables() -> AsyncGenerator[Any, Any]:
    """Define a SCHEMAFULL table with a record<> column before each test."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS fk_articles; REMOVE TABLE IF EXISTS fk_authors;")
    await client.query("DEFINE TABLE fk_authors SCHEMALESS;")
    await client.query(
        "DEFINE TABLE fk_articles SCHEMAFULL;"
        "DEFINE FIELD title ON fk_articles TYPE string;"
        "DEFINE FIELD author ON fk_articles TYPE record<fk_authors>;"
    )
    await client.query("CREATE fk_authors:alice SET name = 'Alice';")
    yield
    await client.query("REMOVE TABLE IF EXISTS fk_articles; REMOVE TABLE IF EXISTS fk_authors;")


@pytest.mark.integration
async def test_save_accepted_by_record_column() -> None:
    """A foreign key must be written as a record link, not a string."""
    article = Article(title="Hello", author="fk_authors:alice")
    await article.save()

    loaded = await Article.objects().get(article.get_id())
    assert loaded.author == "fk_authors:alice"


@pytest.mark.integration
async def test_filter_matches_existing_row() -> None:
    """A filter on a record<> column must not silently return nothing."""
    client = await SurrealDBConnectionManager.get_client()
    # Created server-side so the write path is not involved.
    await client.query("CREATE fk_articles:a1 SET title = 'Hello', author = fk_authors:alice;")

    found = await Article.objects().filter(author="fk_authors:alice").exec()
    assert len(found) == 1
    assert found[0].title == "Hello"


@pytest.mark.integration
async def test_filter_in_matches_existing_row() -> None:
    """Collection lookups are converted element-wise."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("CREATE fk_articles:a1 SET title = 'Hello', author = fk_authors:alice;")

    found = await Article.objects().filter(author__in=["fk_authors:alice", "fk_authors:bob"]).exec()
    assert len(found) == 1


@pytest.mark.integration
async def test_save_and_filter_by_bare_id() -> None:
    """A bare ID is qualified with the target model's table."""
    article = Article(title="Hello", author="alice")
    await article.save()

    found = await Article.objects().filter(author="alice").exec()
    assert len(found) == 1
    assert found[0].author == "fk_authors:alice"


@pytest.mark.integration
async def test_save_and_filter_by_instance() -> None:
    """The related object can be used directly, Django-style."""
    author = await Author.objects().get("alice")

    article = Article(title="Hello", author=author)
    await article.save()

    found = await Article.objects().filter(author=author).exec()
    assert len(found) == 1
    assert found[0].title == "Hello"


@pytest.mark.integration
async def test_filter_by_record_id() -> None:
    """A RecordId passes straight through to the binding."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("CREATE fk_articles:a1 SET title = 'Hello', author = fk_authors:alice;")

    found = await Article.objects().filter(author=RecordId(table="fk_authors", id="alice")).exec()
    assert len(found) == 1


@pytest.mark.integration
async def test_filter_in_mixed_forms() -> None:
    """Instance, full string, bare ID, and RecordId can be mixed in __in."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        "CREATE fk_authors:bob SET name = 'Bob';"
        "CREATE fk_articles:a1 SET title = 'ByAlice', author = fk_authors:alice;"
        "CREATE fk_articles:a2 SET title = 'ByBob', author = fk_authors:bob;"
    )
    alice = await Author.objects().get("alice")

    found = await Article.objects().filter(author__in=[alice, "bob"]).exec()
    assert len(found) == 2

    found = (
        await Article.objects()
        .filter(
            author__in=["fk_authors:alice", RecordId(table="fk_authors", id="bob")],
        )
        .exec()
    )
    assert len(found) == 2


@pytest.mark.integration
async def test_filter_not_in() -> None:
    """not_in excludes converted record links."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        "CREATE fk_authors:bob SET name = 'Bob';"
        "CREATE fk_articles:a1 SET title = 'ByAlice', author = fk_authors:alice;"
        "CREATE fk_articles:a2 SET title = 'ByBob', author = fk_authors:bob;"
    )

    found = await Article.objects().filter(author__not_in=["fk_authors:alice"]).exec()
    assert len(found) == 1
    assert found[0].title == "ByBob"


@pytest.mark.integration
async def test_filter_q_objects() -> None:
    """OR / NOT compositions convert foreign keys too."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        "CREATE fk_authors:bob SET name = 'Bob';"
        "CREATE fk_authors:carol SET name = 'Carol';"
        "CREATE fk_articles:a1 SET title = 'ByAlice', author = fk_authors:alice;"
        "CREATE fk_articles:a2 SET title = 'ByBob', author = fk_authors:bob;"
        "CREATE fk_articles:a3 SET title = 'ByCarol', author = fk_authors:carol;"
    )

    found = await Article.objects().filter(Q(author="alice") | Q(author="fk_authors:bob")).exec()
    assert {a.title for a in found} == {"ByAlice", "ByBob"}

    found = await Article.objects().filter(~Q(author="fk_authors:carol")).exec()
    assert {a.title for a in found} == {"ByAlice", "ByBob"}


@pytest.mark.integration
async def test_bulk_update_accepted_by_record_column() -> None:
    """bulk_update() writes the foreign key as a record link."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query(
        "CREATE fk_authors:bob SET name = 'Bob';CREATE fk_articles:a1 SET title = 'Hello', author = fk_authors:alice;"
    )

    updated = await Article.objects().filter(title="Hello").bulk_update({"author": "bob"})
    assert updated == 1

    found = await Article.objects().filter(author="fk_authors:bob").exec()
    assert len(found) == 1


@pytest.mark.integration
async def test_merge_accepted_by_record_column() -> None:
    """merge() writes the record link too."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("CREATE fk_authors:bob SET name = 'Bob';")

    article = Article(title="Hello", author="fk_authors:alice")
    await article.save()
    await article.merge(author="fk_authors:bob", refresh=False)

    found = await Article.objects().filter(author="fk_authors:bob").exec()
    assert len(found) == 1
