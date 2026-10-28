"""
Integration tests for #294 on the v2 line, against a live SurrealDB 2.x server.

An optional ``ForeignKey`` (``ForeignKey("X") | None``,
``Optional[ForeignKey("X")]``) was never converted to a record link. Observed
on SurrealDB 2.7.0 before the fix:

- ``save()`` into an ``option<record<...>>`` column was rejected
  (``Found '...:alice' for field `author` ... but expected a
  option<record<...>>``);
- on a SCHEMALESS table the string ``'table:id'`` was stored instead of a link;
- ``filter(author=...)``, ``author__in`` and the ``Q`` form bound a string and
  returned no rows for a record that exists.

Run with: pytest -m integration tests/test_issue_294_v2_integration.py
"""

from typing import Any, Optional

import pytest

from src import surreal_orm
from src.surreal_orm import BaseSurrealModel, Q, SurrealConfigDict
from src.surreal_orm.fields import ForeignKey
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_294_v2"
AUTHORS = "i294_authors"
PIPE_BOOKS = "i294_pipe_books"
OPTIONAL_BOOKS = "i294_optional_books"
NOTES = "i294_notes"


class I294Author(BaseSurrealModel):
    """The target of every foreign key here."""

    model_config = SurrealConfigDict(table_name=AUTHORS)

    id: str | None = None
    name: str = "a"


class I294PipeBook(BaseSurrealModel):
    """``ForeignKey(...) | None`` on a SCHEMAFULL ``option<record<...>>`` column."""

    model_config = SurrealConfigDict(table_name=PIPE_BOOKS)

    id: str | None = None
    title: str = "t"
    author: ForeignKey("I294Author") | None = None


class I294OptionalBook(BaseSurrealModel):
    """``Optional[ForeignKey(...)]`` on the same column shape."""

    model_config = SurrealConfigDict(table_name=OPTIONAL_BOOKS)

    id: str | None = None
    title: str = "t"
    author: Optional[ForeignKey("I294Author")] = None  # the Optional spelling is the case under test


class I294Note(BaseSurrealModel):
    """SCHEMALESS: a string would be accepted, so only the stored type shows the defect."""

    model_config = SurrealConfigDict(table_name=NOTES)

    id: str | None = None
    author: ForeignKey("I294Author") | None = None


BOOK_MODELS = [I294PipeBook, I294OptionalBook]
BOOK_IDS = ["pipe", "Optional"]


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    """Point the ORM at the test database."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


async def _run(sql: str) -> list[Any]:
    """Execute *sql*, fail on any rejected statement, return each result."""
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value != "OK"]
    assert errors == [], f"{errors}\nSQL: {sql}"
    return [r.result for r in response.results]


@pytest.fixture(autouse=True)
async def schema():
    """Fresh tables per test; the books are SCHEMAFULL with an option<record<...>> link."""
    drop = "".join(f"REMOVE TABLE IF EXISTS {t};" for t in (AUTHORS, PIPE_BOOKS, OPTIONAL_BOOKS, NOTES))
    await _run(drop)
    ddl = [f"DEFINE TABLE {AUTHORS} SCHEMALESS;", f"CREATE {AUTHORS}:alice SET name = 'Alice';"]
    ddl += [f"CREATE {AUTHORS}:bob SET name = 'Bob';"]
    for table in (PIPE_BOOKS, OPTIONAL_BOOKS):
        ddl += [
            f"DEFINE TABLE {table} SCHEMAFULL;",
            f"DEFINE FIELD title ON {table} TYPE string;",
            f"DEFINE FIELD author ON {table} TYPE option<record<{AUTHORS}>>;",
        ]
    await _run("".join(ddl))
    yield
    await _run(drop)


async def _stored(table: str, record: str) -> dict[str, Any]:
    """The stored link and whether the server sees a record, not a string."""
    (rows,) = await _run(
        f"SELECT type::is::record(author) AS is_record, author = {AUTHORS}:alice AS is_alice FROM {table}:{record};"
    )
    assert len(rows) == 1
    row: dict[str, Any] = rows[0]
    return row


@pytest.mark.integration
@pytest.mark.parametrize("model", BOOK_MODELS, ids=BOOK_IDS)
@pytest.mark.parametrize(
    "value",
    [f"{AUTHORS}:alice", "alice", I294Author(id="alice")],
    ids=["full-id", "bare-id", "instance"],
)
async def test_save_stores_a_record_link(model: type[BaseSurrealModel], value: Any) -> None:
    """The column accepts the write, and holds the link to alice."""
    await model(id="b1", title="t", author=value).save()

    assert await _stored(model.get_table_name(), "b1") == {"is_record": True, "is_alice": True}


@pytest.mark.integration
@pytest.mark.parametrize("model", BOOK_MODELS, ids=BOOK_IDS)
async def test_save_without_a_link_stores_none(model: type[BaseSurrealModel]) -> None:
    """None stays NONE — option<> accepts it, nothing is invented."""
    await model(id="b1", title="t").save()

    (rows,) = await _run(f"SELECT type::is::none(author) AS is_none FROM {model.get_table_name()}:b1;")
    assert rows == [{"is_none": True}]


@pytest.mark.integration
@pytest.mark.parametrize("model", BOOK_MODELS, ids=BOOK_IDS)
async def test_merge_stores_a_record_link(model: type[BaseSurrealModel]) -> None:
    """merge() through the optional foreign key writes a link, not a string."""
    await _run(f"CREATE {model.get_table_name()}:b1 SET title = 't';")
    book = await model.objects().get("b1")

    await book.merge(author="alice")

    assert await _stored(model.get_table_name(), "b1") == {"is_record": True, "is_alice": True}


@pytest.mark.integration
async def test_schemaless_save_stores_a_link_not_a_string() -> None:
    """Where a string is accepted, it must still not be what is stored."""
    await I294Note(id="n1", author=f"{AUTHORS}:alice").save()

    assert await _stored(NOTES, "n1") == {"is_record": True, "is_alice": True}


@pytest.mark.integration
@pytest.mark.parametrize("model", BOOK_MODELS, ids=BOOK_IDS)
class TestFilterFindsServerSideRows:
    """Rows written by the server hold real links; the filters must match them."""

    @pytest.fixture(autouse=True)
    async def rows(self, model: type[BaseSurrealModel]) -> None:
        table = model.get_table_name()
        await _run(
            f"CREATE {table}:by_alice SET title = 'a', author = {AUTHORS}:alice;"
            f"CREATE {table}:by_bob SET title = 'b', author = {AUTHORS}:bob;"
            f"CREATE {table}:orphan SET title = 'o';"
        )

    @staticmethod
    def _ids(rows: list[Any]) -> list[str | None]:
        return sorted(row.get_id() for row in rows)

    @pytest.mark.parametrize(
        "value",
        [f"{AUTHORS}:alice", "alice", I294Author(id="alice")],
        ids=["full-id", "bare-id", "instance"],
    )
    async def test_exact(self, model: type[BaseSurrealModel], value: Any) -> None:
        assert self._ids(await model.objects().filter(author=value).exec()) == ["by_alice"]

    async def test_in(self, model: type[BaseSurrealModel]) -> None:
        rows = await model.objects().filter(author__in=["alice", f"{AUTHORS}:bob"]).exec()

        assert self._ids(rows) == ["by_alice", "by_bob"]

    async def test_not_in(self, model: type[BaseSurrealModel]) -> None:
        rows = await model.objects().filter(author__not_in=["alice"]).exec()

        assert self._ids(rows) == ["by_bob", "orphan"]

    async def test_q(self, model: type[BaseSurrealModel]) -> None:
        rows = await model.objects().filter(Q(author="alice") | Q(author__in=["bob"])).exec()

        assert self._ids(rows) == ["by_alice", "by_bob"]
