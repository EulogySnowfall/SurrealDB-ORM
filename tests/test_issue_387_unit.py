"""Unit tests for #387: ORM writes with a non-identifier alias inside an ``HTTPTransaction``.

#334 made ``HTTPTransaction`` check each key of ``data`` when a statement is
queued: a key that is not a plain SurrealQL identifier raises ``ValueError``
naming it, and nothing is queued. The ORM reaches those methods with keys it
does not choose itself: ``save()``, ``update()`` and ``merge()`` send the
model's *aliases* (``model_dump(by_alias=True)``), and ``relate()`` sends its
``**edge_data`` keywords as they are. These tests drive that path end to end on
a mocked connection, so the check stays wired to every ORM entry point that
queues bound data:

- ``save(tx=)`` of a new record with an id (``tx.upsert``), of a new record
  without an id (``tx.create``) and of a persisted record (``tx.merge``);
- ``update(tx=)`` and ``merge(tx=, ...)`` (``tx.merge``);
- ``relate(..., tx=)`` with edge data (``tx.relate``).

Values are scalars only: a ``SurrealFunc`` or a nested dict/list sends
``save()`` / ``merge()`` through ``tx.query()`` with a ``SET`` clause, which has
its own identifier check and is not covered here.

The ``pre_save`` / ``pre_update`` signals fire before the rejection; these
tests deliberately say nothing about them.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict
from src.surreal_sdk.transaction import HTTPTransaction
from src.surreal_sdk.types import QueryResponse


class I387Person(BaseSurrealModel):
    """A column whose alias is not an identifier (``first-name``)."""

    model_config = SurrealConfigDict(table_name="i387_people")

    id: str | None = None
    first_name: str | None = Field(default=None, alias="first-name")


class I387Contact(BaseSurrealModel):
    """Control: the same column under an identifier alias (``firstName``)."""

    model_config = SurrealConfigDict(table_name="i387_contacts")

    id: str | None = None
    first_name: str | None = Field(default=None, alias="firstName")


@pytest.fixture
def connection() -> MagicMock:
    conn = MagicMock()
    conn.query = AsyncMock(return_value=QueryResponse(results=[], raw=[]))
    return conn


async def _open(conn: MagicMock) -> HTTPTransaction:
    tx = HTTPTransaction(conn)
    await tx._begin()
    return tx


def _persisted(model: type[BaseSurrealModel], **data: Any) -> Any:
    """An instance that ``save()`` treats as already in the database (merge path)."""
    instance = model(id="ada", **data)
    instance._db_persisted = True
    return instance


def _key_error(key: str) -> str:
    return re.escape(f"Invalid data key: {key!r}")


Write = Callable[[type[BaseSurrealModel], str, HTTPTransaction], Awaitable[Any]]

#: One ORM write per transaction method that receives the model's column keys.
#: Each takes the model class and the column's wire name (its alias).
WRITES: dict[str, Write] = {
    "save_new_with_id_upsert": lambda m, key, tx: m(id="ada", **{key: "Ada"}).save(tx=tx),
    "save_new_without_id_create": lambda m, key, tx: m(**{key: "Ada"}).save(tx=tx),
    "save_persisted_merge": lambda m, key, tx: _persisted(m, **{key: "Ada"}).save(tx=tx),
    "update_merge": lambda m, key, tx: _persisted(m, **{key: "Ada"}).update(tx=tx),
    "merge": lambda m, key, tx: m(id="ada").merge(tx=tx, **{key: "Ada"}),
}


class TestNonIdentifierAliasIsRejected:
    @pytest.mark.parametrize("write", WRITES)
    async def test_raises_naming_the_alias_and_queues_nothing(self, connection: MagicMock, write: str) -> None:
        tx = await _open(connection)
        with pytest.raises(ValueError, match=_key_error("first-name")):
            await WRITES[write](I387Person, "first-name", tx)
        assert tx._statements == []

    async def test_relate_edge_data_key_raises_and_queues_nothing(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        with pytest.raises(ValueError, match=_key_error("since-year")):
            await I387Person(id="ada").relate("i387_knows", I387Person(id="bob"), tx=tx, **{"since-year": 2020})
        assert tx._statements == []

    @pytest.mark.parametrize("write", WRITES)
    async def test_earlier_statement_is_kept_and_only_it_commits(self, connection: MagicMock, write: str) -> None:
        tx = await _open(connection)
        await I387Contact(id="before", **{"firstName": "Bea"}).save(tx=tx)
        with pytest.raises(ValueError, match=_key_error("first-name")):
            await WRITES[write](I387Person, "first-name", tx)
        assert len(tx._statements) == 1
        await tx.commit()
        sql = connection.query.await_args.args[0]
        assert "first-name" not in sql
        assert "i387_people" not in sql


class TestIdentifierAliasQueues:
    """The same writes on an identifier alias queue the expected statement, keyed by the alias."""

    EXPECTED: dict[str, tuple[str, dict[str, Any]]] = {
        "save_new_with_id_upsert": ("UPSERT i387_contacts:ada SET firstName = $firstName;", {"firstName": "Ada"}),
        "save_new_without_id_create": ("CREATE i387_contacts SET firstName = $firstName;", {"firstName": "Ada"}),
        "save_persisted_merge": ("UPDATE i387_contacts:ada MERGE { firstName: $firstName };", {"firstName": "Ada"}),
        "update_merge": ("UPDATE i387_contacts:ada MERGE { firstName: $firstName };", {"firstName": "Ada"}),
        "merge": ("UPDATE i387_contacts:ada MERGE { firstName: $firstName };", {"firstName": "Ada"}),
    }

    @pytest.mark.parametrize("write", WRITES)
    async def test_queues_the_alias_keyed_statement(self, connection: MagicMock, write: str) -> None:
        tx = await _open(connection)
        await WRITES[write](I387Contact, "firstName", tx)
        assert [(s.sql, s.vars) for s in tx._statements] == [self.EXPECTED[write]]

    async def test_relate_with_identifier_edge_data(self, connection: MagicMock) -> None:
        tx = await _open(connection)
        edge = await I387Contact(id="ada").relate("i387_knows", I387Contact(id="bob"), tx=tx, since_year=2020)
        assert edge == {"in": "i387_contacts:ada", "out": "i387_contacts:bob", "since_year": 2020}
        assert [(s.sql, s.vars) for s in tx._statements] == [
            (
                "RELATE i387_contacts:ada->i387_knows->i387_contacts:bob SET since_year = $since_year;",
                {"since_year": 2020},
            )
        ]
