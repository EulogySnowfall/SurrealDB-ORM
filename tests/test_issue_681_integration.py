"""
Integration test for #681: ``merge()`` writes an aliased field under its alias.

Before the fix, ``merge(first_name=...)`` on a field declared
``Field(alias="first-name")`` wrote a second column ``first_name`` beside the
``first-name`` column that ``save()`` writes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import Field

from src import surreal_orm
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.surreal_function import SurrealFunc
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = "test_issue_681"
TABLE = "i681_people"


class I681Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=TABLE)

    id: str | None = None
    first_name: str = Field(default="", alias="first-name")
    age: int = 0


@pytest.fixture(autouse=True)
async def i681_db() -> AsyncGenerator[None, None]:
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(f"REMOVE TABLE IF EXISTS {TABLE};")
    try:
        yield
    finally:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(f"REMOVE TABLE IF EXISTS {TABLE};")
        await surreal_orm.SurrealDBConnectionManager.close_connection()


async def _record() -> dict[str, Any]:
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    records = (await client.query(f"SELECT * FROM {TABLE}:a;")).all_records
    assert len(records) == 1
    record: dict[str, Any] = records[0]
    return record


@pytest.mark.parametrize(
    "value",
    ["Bob", SurrealFunc("'Bob'")],
    ids=["rpc-merge", "set-clause"],
)
async def test_merge_by_field_name_writes_the_alias_column(value: Any) -> None:
    person = I681Person(id="a", first_name="Ann", age=3)
    await person.save()

    await person.merge(first_name=value)

    assert person.first_name == "Bob"
    fetched = await I681Person.objects().get("a")
    assert fetched.first_name == "Bob"
    record = await _record()
    assert record["first-name"] == "Bob"
    assert "first_name" not in record
    assert record["age"] == 3


async def test_merge_by_alias_writes_the_alias_column() -> None:
    person = I681Person(id="a", first_name="Ann")
    await person.save()

    await person.merge(**{"first-name": "Cy"})

    assert person.first_name == "Cy"
    assert (await I681Person.objects().get("a")).first_name == "Cy"
    assert "first_name" not in await _record()


@pytest.mark.parametrize(
    "kwargs",
    [{"first_name": "Dee"}, {"first-name": "Dee"}],
    ids=["field-name", "alias"],
)
async def test_merge_in_a_transaction_writes_the_alias_column(kwargs: dict[str, Any]) -> None:
    person = I681Person(id="a", first_name="Ann", age=3)
    await person.save()

    async with await surreal_orm.SurrealDBConnectionManager.transaction() as tx:
        await person.merge(tx=tx, **kwargs)
        assert person.first_name == "Dee"

    assert (await I681Person.objects().get("a")).first_name == "Dee"
    record = await _record()
    assert record["first-name"] == "Dee"
    assert "first_name" not in record
    assert record["age"] == 3
