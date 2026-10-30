"""
Integration tests for #413 on the v2 line, against a live SurrealDB 2.x server.

``SearchHighlight`` escaped the apostrophe of its tags but not the backslash,
so a tag containing or ending in ``\\`` did not render as the text it holds:
the query failed to parse. The unit tests in ``tests/test_search.py`` pin the
rendered literal; these run a full-text query annotated with such a highlight
and read back the rows and the database definition (``INFO FOR DB``).

Run with: pytest -m integration tests/test_issue_413_v2_integration.py
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from src import surreal_orm
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.search import SearchHighlight
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_413_v2"
DOCS = "i413_docs"
OTHER = "i413_other"
ANALYZER = "i413_az"


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


async def _info_for_db() -> dict[str, Any]:
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    info = await client.query("INFO FOR DB;")
    result = info.first_result.result
    assert isinstance(result, dict)
    return result


async def _run(sql: str) -> None:
    """Execute *sql* and fail on any statement the server rejected.

    ``client.query()`` only raises on an RPC failure; a rejected statement
    comes back as ``status: ERR`` inside a successful response.
    """
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    response = await client.query(sql)
    errors = [str(r.result) for r in response.results if r.status.value != "OK"]
    assert errors == [], f"{errors}\nSQL: {sql}"


@pytest.fixture(autouse=True)
async def clean_database() -> AsyncIterator[None]:
    """Empty this file's database before and after each test."""

    async def cleanup() -> None:
        info = await _info_for_db()
        statements = [f"REMOVE TABLE IF EXISTS {name};" for name in set(info.get("tables", {})) | {DOCS, OTHER}]
        statements.append(f"REMOVE ANALYZER IF EXISTS {ANALYZER};")
        await _run(" ".join(statements))

    await cleanup()
    yield
    await cleanup()


class I413Doc(BaseSurrealModel):
    """A document indexed for full-text search with highlights."""

    model_config = SurrealConfigDict(table_name=DOCS)

    id: str | None = None
    title: str = ""


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("open_tag", "close_tag"),
    [
        ("a\\", "</b>"),
        ("<b>", "\\"),
        ("it's \\", "\\'"),
        (f"a\\'; DEFINE TABLE {OTHER}; --", "\\"),
    ],
)
async def test_highlight_tags_are_returned_as_written(open_tag: str, close_tag: str) -> None:
    """The query runs, the tags come back as written, and the schema is unchanged."""
    await _run(
        f"DEFINE ANALYZER {ANALYZER} TOKENIZERS blank FILTERS lowercase;"
        f" DEFINE INDEX i413_ft ON {DOCS} FIELDS title SEARCH ANALYZER {ANALYZER} BM25 HIGHLIGHTS;"
        f" CREATE {DOCS}:one SET title = 'quantum physics';"
    )
    before = await _info_for_db()

    rows = await I413Doc.objects().search(title="quantum").annotate(snippet=SearchHighlight(open_tag, close_tag, 0)).exec()

    assert len(rows) == 1
    assert getattr(rows[0], "snippet", None) == f"{open_tag}quantum{close_tag} physics"
    after = await _info_for_db()
    assert after == before
    assert OTHER not in after["tables"]
