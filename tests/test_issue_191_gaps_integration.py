"""
Integration tests for the #191 gaps, against a live SurrealDB 2.6.x.

The unit tests pin what the encoders emit; only a real server shows that the
JSON protocol writes the *same record* as CBOR. ``str(RecordId)`` renders
``t:123``, which SurrealDB reads as the numeric record — while CBOR writes the
string one, ``t:`123```. Two different rows, no error, and the JSON path was the
one this PR set out to repair.

Run with: pytest -m integration tests/test_issue_191_gaps_integration.py
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src import surreal_orm
from src.surreal_orm import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.fields import ForeignKey
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_191_gaps"


class JsonTarget(BaseSurrealModel):
    """Target of the foreign key."""

    model_config = SurrealConfigDict(table_name="j191_targets")

    id: str | None = None
    name: str = "x"


class JsonHolder(BaseSurrealModel):
    """Holds the foreign key."""

    model_config = SurrealConfigDict(table_name="j191_holders")

    id: str | None = None
    author: ForeignKey("JsonTarget") = None  # type: ignore[valid-type]


@pytest.fixture(autouse=True)
async def json_connection() -> AsyncGenerator[Any, Any]:
    """Drive the ORM over the JSON protocol, which is what regressed."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
        protocol="json",
    )
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS j191_holders; REMOVE TABLE IF EXISTS j191_targets;")
    await client.query("DEFINE TABLE j191_targets SCHEMALESS; CREATE j191_targets:`123` SET name = 'numeric-looking';")
    yield
    await client.query("REMOVE TABLE IF EXISTS j191_holders; REMOVE TABLE IF EXISTS j191_targets;")
    await surreal_orm.SurrealDBConnectionManager.close_connection()
    await surreal_orm.SurrealDBConnectionManager.unset_connection()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_foreign_key_save_works_over_json() -> None:
    """Coercion made every FK save raise TypeError on this protocol."""
    holder = JsonHolder(id="h1", author="j191_targets:alice")
    await holder.save()

    loaded = await JsonHolder.objects().get("h1")

    assert loaded.author == "j191_targets:alice"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_numeric_looking_id_keeps_its_type_over_json() -> None:
    """``str(RecordId)`` would write t:123, the numeric record, not t:`123`."""
    holder = JsonHolder(id="h2", author="j191_targets:123")
    await holder.save()

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    rows = await client.query("SELECT meta::id(author) IS 123 AS numeric FROM j191_holders:h2;")

    assert rows.all_records[0]["numeric"] is False, "the string id was written as the numeric record"
