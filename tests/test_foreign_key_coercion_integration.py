"""Integration tests for ForeignKey fields against a real ``record<>`` column.

Every test here fails without the coercion: the write is rejected by SurrealDB,
and the filter returns an empty result for a row that demonstrably exists.
"""

from collections.abc import AsyncGenerator
from typing import Any, Optional

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


# ==================== Optional foreign keys (#268) ====================
#
# ``ForeignKey("X") | None`` hid the marker from the record-link coercion, so the
# write was rejected by an ``option<record<...>>`` column and a filter silently
# matched nothing.  The rows the filters look for are created server-side, so
# only the filter path is under test there.


class Fki268Author(BaseSurrealModel):
    """Target of the optional foreign keys below."""

    model_config = SurrealConfigDict(table_name="fki268_authors")

    id: str | None = None
    name: str = Field(default="")


class Fki268PipePost(BaseSurrealModel):
    """ForeignKey wrapped in a redundant ``| None``."""

    model_config = SurrealConfigDict(table_name="fki268_posts")

    id: str | None = None
    title: str = Field(default="")
    author: ForeignKey("Fki268Author") | None = None  # type: ignore[valid-type]


class Fki268OptionalPost(BaseSurrealModel):
    """ForeignKey wrapped in ``Optional[...]``."""

    model_config = SurrealConfigDict(table_name="fki268_posts")

    id: str | None = None
    title: str = Field(default="")
    author: Optional[ForeignKey("Fki268Author")] = None  # type: ignore[valid-type]


class Fki268AliasedPost(BaseSurrealModel):
    """Optional ForeignKey stored under the ``editor`` column."""

    model_config = SurrealConfigDict(table_name="fki268_posts")

    id: str | None = None
    title: str = Field(default="")
    editor_ref: ForeignKey("Fki268Author") | None = Field(default=None, alias="editor")  # type: ignore[valid-type]


_OPTIONAL_FK_MODELS = pytest.mark.parametrize(
    "model",
    [Fki268PipePost, Fki268OptionalPost],
    ids=["pipe-none", "optional"],
)


@pytest.fixture
async def optional_fk_tables() -> AsyncGenerator[Any, Any]:
    """An ``option<record<...>>`` column, with authors and one post created server-side."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS fki268_posts; REMOVE TABLE IF EXISTS fki268_authors;")
    await client.query(
        "DEFINE TABLE fki268_authors SCHEMALESS;"
        "DEFINE TABLE fki268_posts SCHEMAFULL;"
        "DEFINE FIELD title ON fki268_posts TYPE string;"
        "DEFINE FIELD author ON fki268_posts TYPE option<record<fki268_authors>>;"
        "DEFINE FIELD editor ON fki268_posts TYPE option<record<fki268_authors>>;"
        "CREATE fki268_authors:alice SET name = 'Alice';"
        "CREATE fki268_authors:bob SET name = 'Bob';"
        "CREATE fki268_posts:p1 SET title = 'ByAlice', author = fki268_authors:alice, editor = fki268_authors:alice;"
        "CREATE fki268_posts:p2 SET title = 'ByBob', author = fki268_authors:bob, editor = fki268_authors:bob;"
    )
    yield client
    await client.query("REMOVE TABLE IF EXISTS fki268_posts; REMOVE TABLE IF EXISTS fki268_authors;")


async def _is_record_link(client: Any, record: str, column: str) -> bool:
    """Ask the server whether *column* of *record* holds a record link."""
    result = await client.query(f"SELECT type::is_record({column}) AS linked FROM {record};")
    return bool(result.all_records == [{"linked": True}])


@pytest.mark.integration
@_OPTIONAL_FK_MODELS
async def test_optional_fk_save_stores_a_record_link(
    model: type[BaseSurrealModel],
    optional_fk_tables: Any,
) -> None:
    post = model(id="s1", title="Saved", author="fki268_authors:alice")
    await post.save()

    assert await _is_record_link(optional_fk_tables, "fki268_posts:s1", "author")
    loaded = await model.objects().get("s1")
    assert loaded.author == "fki268_authors:alice"  # type: ignore[attr-defined]


@pytest.mark.integration
@_OPTIONAL_FK_MODELS
async def test_optional_fk_save_by_bare_id_and_instance(
    model: type[BaseSurrealModel],
    optional_fk_tables: Any,
) -> None:
    alice = await Fki268Author.objects().get("alice")
    await model(id="s1", title="Bare", author="alice").save()
    await model(id="s2", title="Instance", author=alice).save()

    assert await _is_record_link(optional_fk_tables, "fki268_posts:s1", "author")
    assert await _is_record_link(optional_fk_tables, "fki268_posts:s2", "author")


@pytest.mark.integration
@_OPTIONAL_FK_MODELS
async def test_optional_fk_save_without_value(
    model: type[BaseSurrealModel],
    optional_fk_tables: Any,
) -> None:
    """None stays NONE: the optional column accepts the absent link."""
    await model(id="s1", title="Anonymous").save()

    result = await optional_fk_tables.query("SELECT author FROM fki268_posts:s1;")
    assert result.all_records[0].get("author") is None


@pytest.mark.integration
@_OPTIONAL_FK_MODELS
async def test_optional_fk_merge_stores_a_record_link(
    model: type[BaseSurrealModel],
    optional_fk_tables: Any,
) -> None:
    post = model(id="s1", title="Saved")
    await post.save()
    await post.merge(author="bob", refresh=False)

    assert await _is_record_link(optional_fk_tables, "fki268_posts:s1", "author")
    found = await model.objects().filter(author="fki268_authors:bob").exec()
    assert {p.title for p in found} == {"ByBob", "Saved"}  # type: ignore[attr-defined]


@pytest.mark.integration
@_OPTIONAL_FK_MODELS
@pytest.mark.parametrize("value", ["fki268_authors:alice", "alice"], ids=["full-id", "bare-id"])
async def test_optional_fk_filter_matches_existing_row(
    model: type[BaseSurrealModel],
    value: str,
    optional_fk_tables: Any,
) -> None:
    found = await model.objects().filter(author=value).exec()
    assert [p.title for p in found] == ["ByAlice"]  # type: ignore[attr-defined]


@pytest.mark.integration
@_OPTIONAL_FK_MODELS
async def test_optional_fk_filter_by_instance(
    model: type[BaseSurrealModel],
    optional_fk_tables: Any,
) -> None:
    alice = await Fki268Author.objects().get("alice")
    found = await model.objects().filter(author=alice).exec()
    assert [p.title for p in found] == ["ByAlice"]  # type: ignore[attr-defined]


@pytest.mark.integration
@_OPTIONAL_FK_MODELS
async def test_optional_fk_filter_in_and_not_in(
    model: type[BaseSurrealModel],
    optional_fk_tables: Any,
) -> None:
    found = await model.objects().filter(author__in=["alice", "fki268_authors:bob"]).exec()
    assert {p.title for p in found} == {"ByAlice", "ByBob"}  # type: ignore[attr-defined]

    found = await model.objects().filter(author__not_in=["alice"]).exec()
    assert [p.title for p in found] == ["ByBob"]  # type: ignore[attr-defined]


@pytest.mark.integration
@_OPTIONAL_FK_MODELS
async def test_optional_fk_filter_q_objects(
    model: type[BaseSurrealModel],
    optional_fk_tables: Any,
) -> None:
    found = await model.objects().filter(Q(author="alice") | Q(author="fki268_authors:bob")).exec()
    assert {p.title for p in found} == {"ByAlice", "ByBob"}  # type: ignore[attr-defined]

    found = await model.objects().filter(~Q(author="bob")).exec()
    assert [p.title for p in found] == ["ByAlice"]  # type: ignore[attr-defined]


@pytest.mark.integration
async def test_optional_fk_aliased_column(optional_fk_tables: Any) -> None:
    """An aliased optional ForeignKey is written and filtered under its column name."""
    await Fki268AliasedPost(id="s1", title="Edited", editor="bob").save()
    assert await _is_record_link(optional_fk_tables, "fki268_posts:s1", "editor")

    found = await Fki268AliasedPost.objects().filter(Q(editor="alice") | Q(editor__in=["fki268_authors:bob"])).exec()
    assert {p.title for p in found} == {"ByAlice", "ByBob", "Edited"}
