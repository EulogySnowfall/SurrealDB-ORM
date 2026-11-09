"""
Unit tests for #681: ``merge()`` writes an aliased field under its alias.

``save()`` and ``update()`` dump the model ``by_alias``, so a field declared
``Field(alias="first-name")`` lives in the ``first-name`` column. ``merge()``
took its keywords as column names, so ``merge(first_name=...)`` wrote a second
column ``first_name`` that the alias column never saw. Each write path is
covered here with a mocked client or transaction: the plain RPC ``merge``, the
``UPDATE ... SET`` path taken for a :class:`SurrealFunc`, and ``tx=``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field

from src.surreal_orm import signals as model_signals
from src.surreal_orm.fields.relation import ForeignKey
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.surreal_function import SurrealFunc
from src.surreal_sdk.transaction import HTTPTransaction
from src.surreal_sdk.types import QueryResponse
from surreal_sdk.protocol.cbor import RecordId


class U681Owner(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u681_owners")

    id: str | None = None


class U681Person(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u681_people")

    id: str | None = None
    first_name: str = Field(default="", alias="first-name")
    nick: str = Field(default="", alias="nick_col")
    age: int = 0
    owner: ForeignKey("U681Owner") | None = Field(default=None, alias="owner-id")  # type: ignore[valid-type]


SPELLINGS = pytest.mark.parametrize(
    "kwargs",
    [{"first_name": "Bob"}, {"first-name": "Bob"}],
    ids=["field-name", "alias"],
)


def _person() -> U681Person:
    person = U681Person(id="a", first_name="Ann")
    person._db_persisted = True
    return person


def _client() -> AsyncMock:
    client = AsyncMock()
    client.merge = AsyncMock(return_value=MagicMock(is_empty=False))
    client.query = AsyncMock(return_value=MagicMock(is_empty=False))
    return client


def _manager(client: AsyncMock) -> Any:
    return patch(
        "src.surreal_orm.model_base.SurrealDBConnectionManager",
        new=MagicMock(get_client=AsyncMock(return_value=client)),
    )


def _tx() -> MagicMock:
    tx = MagicMock()
    tx.defers_results = True
    tx.merge = AsyncMock(return_value=MagicMock(is_empty=True))
    tx.query = AsyncMock(return_value=MagicMock(is_empty=True))
    return tx


class TestRpcMerge:
    @SPELLINGS
    async def test_writes_the_alias_column(self, kwargs: dict[str, Any]) -> None:
        client = _client()
        with _manager(client):
            await _person().merge(refresh=False, **kwargs)

        thing, data = client.merge.call_args[0]
        assert thing == "u681_people:a"
        assert data == {"first-name": "Bob"}

    async def test_identifier_alias(self) -> None:
        client = _client()
        with _manager(client):
            await _person().merge(refresh=False, nick="b")

        assert client.merge.call_args[0][1] == {"nick_col": "b"}

    async def test_unknown_keyword_and_unaliased_field_are_written_as_given(self) -> None:
        client = _client()
        with _manager(client):
            await _person().merge(refresh=False, age=3, extra_col=1, **{"odd-key": 2})

        assert client.merge.call_args[0][1] == {"age": 3, "extra_col": 1, "odd-key": 2}

    @pytest.mark.parametrize("key", ["owner", "owner-id"])
    async def test_foreign_key_is_coerced_under_the_alias(self, key: str) -> None:
        client = _client()
        with _manager(client):
            await _person().merge(refresh=False, **{key: "bob"})

        assert client.merge.call_args[0][1] == {"owner-id": RecordId(table="u681_owners", id="bob")}

    async def test_signals_carry_the_column_names(self) -> None:
        seen: list[dict[str, Any]] = []

        async def handler(sender: Any, instance: Any, update_fields: dict[str, Any], **kwargs: Any) -> None:
            seen.append(dict(update_fields))

        model_signals.pre_update.connect(U681Person)(handler)
        try:
            with _manager(_client()):
                await _person().merge(refresh=False, first_name="Bob")
        finally:
            model_signals.pre_update.disconnect(handler, sender=U681Person)

        assert seen == [{"first-name": "Bob"}]


class TestSetClauseMerge:
    @pytest.mark.parametrize("key", ["first_name", "first-name"])
    async def test_func_value_writes_the_escaped_alias_column(self, key: str) -> None:
        client = _client()
        with _manager(client):
            await _person().merge(refresh=False, **{key: SurrealFunc("string::uppercase('bob')")})

        query, variables = client.query.call_args[0]
        assert query == "UPDATE u681_people:a SET `first-name` = string::uppercase('bob');"
        assert variables == {}

    @SPELLINGS
    async def test_bound_value_on_alias_column(self, kwargs: dict[str, Any]) -> None:
        client = _client()
        with _manager(client):
            await _person().merge(refresh=False, age=SurrealFunc("age + 1"), **kwargs)

        query, variables = client.query.call_args[0]
        assert query == "UPDATE u681_people:a SET age = age + 1, `first-name` = $_svq_0;"
        assert variables == {"_svq_0": "Bob"}

    async def test_identifier_alias_keeps_its_parameter_name(self) -> None:
        client = _client()
        with _manager(client):
            await _person().merge(refresh=False, nick="b", age=SurrealFunc("age + 1"))

        query, variables = client.query.call_args[0]
        assert query == "UPDATE u681_people:a SET nick_col = $_sv_nick_col, age = age + 1;"
        assert variables == {"_sv_nick_col": "b"}

    async def test_unknown_non_identifier_keyword_is_still_rejected(self) -> None:
        client = _client()
        with _manager(client), pytest.raises(ValueError, match="Invalid field name"):
            await _person().merge(refresh=False, age=SurrealFunc("age + 1"), **{"odd-key": 2})
        client.query.assert_not_called()

    def test_escaped_column_quotes_backslash_and_backtick(self) -> None:
        clause, variables = BaseSurrealModel._build_set_clause({"a`b\\c": 1, "ok": 2}, escaped_columns=frozenset({"a`b\\c"}))
        assert clause == "`a\\`b\\\\c` = $_svq_0, ok = $_sv_ok"
        assert variables == {"_svq_0": 1, "_sv_ok": 2}


class TestTransactionMerge:
    @SPELLINGS
    async def test_writes_the_alias_column_and_updates_the_instance(self, kwargs: dict[str, Any]) -> None:
        tx = _tx()
        person = _person()
        await person.merge(tx=tx, **kwargs)

        tx.merge.assert_not_called()
        query, variables = tx.query.call_args[0]
        assert query == "UPDATE u681_people:a MERGE $_mq;"
        assert variables == {"_mq": {"first-name": "Bob"}}
        assert person.first_name == "Bob"

    async def test_identifier_columns_go_through_tx_merge(self) -> None:
        tx = _tx()
        person = _person()
        await person.merge(tx=tx, nick="b")

        tx.query.assert_not_called()
        assert tx.merge.call_args[0] == ("u681_people:a", {"nick_col": "b"})
        assert person.nick == "b"

    @pytest.mark.parametrize("key", ["owner", "owner-id"])
    async def test_foreign_key_updates_the_instance(self, key: str) -> None:
        tx = _tx()
        person = _person()
        await person.merge(tx=tx, **{key: U681Owner(id="bob")})

        assert tx.query.call_args[0][1] == {"_mq": {"owner-id": RecordId(table="u681_owners", id="bob")}}
        assert person.owner == "u681_owners:bob"

    @pytest.mark.parametrize("key", ["first_name", "first-name"])
    async def test_func_value_goes_through_tx_query(self, key: str) -> None:
        tx = _tx()
        await _person().merge(tx=tx, refresh=False, **{key: SurrealFunc("'x'")})

        query, _variables = tx.query.call_args[0]
        assert query == "UPDATE u681_people:a SET `first-name` = 'x';"


class TestHttpTransactionMerge:
    """A real ``HTTPTransaction``: the one ``SurrealDBConnectionManager.transaction()`` returns.

    ``HTTPTransaction.merge()`` writes each key into the statement text and
    rejects one that is not an identifier, so a non-identifier alias column is
    sent as one bound object instead (``UPDATE ... MERGE $_mq``).
    """

    @staticmethod
    async def _open() -> tuple[HTTPTransaction, MagicMock]:
        connection = MagicMock()
        connection.query = AsyncMock(return_value=QueryResponse(results=[], raw=[]))
        tx = HTTPTransaction(connection)
        await tx._begin()
        return tx, connection

    @SPELLINGS
    async def test_non_identifier_alias_is_bound_as_one_object(self, kwargs: dict[str, Any]) -> None:
        tx, connection = await self._open()
        person = _person()
        await person.merge(tx=tx, **kwargs)

        assert [(s.sql, s.vars) for s in tx._statements] == [
            ("UPDATE u681_people:a MERGE $_mq;", {"_mq": {"first-name": "Bob"}}),
        ]
        assert person.first_name == "Bob"

        await tx.commit()
        sql, variables = connection.query.await_args.args
        assert "UPDATE u681_people:a MERGE $tx_0__mq;" in sql
        assert variables == {"tx_0__mq": {"first-name": "Bob"}}

    async def test_foreign_key_under_a_non_identifier_alias_is_coerced(self) -> None:
        tx, _connection = await self._open()
        person = _person()
        await person.merge(tx=tx, age=4, owner="bob")

        assert [(s.sql, s.vars) for s in tx._statements] == [
            (
                "UPDATE u681_people:a MERGE $_mq;",
                {"_mq": {"age": 4, "owner-id": RecordId(table="u681_owners", id="bob")}},
            ),
        ]
        assert (person.age, person.owner) == (4, "u681_owners:bob")

    async def test_identifier_columns_keep_the_merge_object_literal(self) -> None:
        tx, _connection = await self._open()
        person = _person()
        await person.merge(tx=tx, nick="b", age=4)

        assert [(s.sql, s.vars) for s in tx._statements] == [
            ("UPDATE u681_people:a MERGE { nick_col: $nick_col, age: $age };", {"nick_col": "b", "age": 4}),
        ]
        assert (person.nick, person.age) == ("b", 4)

    @pytest.mark.parametrize(
        "kwargs",
        [{"x-y": 1}, {"a.b": 1}, {"first_name": "Bob", "x-y": 1}],
        ids=["dash", "dotted", "alias-and-unknown"],
    )
    async def test_unknown_non_identifier_key_is_still_rejected(self, kwargs: dict[str, Any]) -> None:
        # Only declared aliases take the bound-object form; any other key that
        # is not an identifier still reaches HTTPTransaction.merge(), which
        # refuses it before queueing anything.
        tx, connection = await self._open()
        person = _person()
        with pytest.raises(ValueError, match="Invalid data key"):
            await person.merge(tx=tx, **kwargs)

        assert tx._statements == []
        assert person.first_name == "Ann"
        connection.query.assert_not_called()


class TestBothSpellings:
    @pytest.mark.parametrize("use_tx", [False, True], ids=["client", "tx"])
    async def test_raises_before_any_request(self, use_tx: bool) -> None:
        client = _client()
        tx = _tx()
        person = _person()
        with _manager(client), pytest.raises(ValueError, match="first_name.*first-name"):
            await person.merge(tx=tx if use_tx else None, first_name="Bob", **{"first-name": "Cy"})

        client.merge.assert_not_called()
        client.query.assert_not_called()
        tx.merge.assert_not_called()
        tx.query.assert_not_called()
        assert person.first_name == "Ann"


class TestReadBack:
    """``refresh()`` reads a record through ``_update_from_db()``, which skipped alias columns."""

    def test_alias_column_is_read_into_the_field(self) -> None:
        person = _person()
        person._update_from_db({"id": RecordId(table="u681_people", id="a"), "first-name": "Bob", "nick_col": "b"})
        assert (person.first_name, person.nick) == ("Bob", "b")

    def test_alias_column_wins_over_a_stray_field_name_column(self) -> None:
        person = _person()
        person._update_from_db({"first-name": "Bob", "first_name": "stale"})
        assert person.first_name == "Bob"

    def test_field_name_column_is_read_when_the_alias_column_is_absent(self) -> None:
        person = _person()
        person._update_from_db({"first_name": "Old"})
        assert person.first_name == "Old"

    async def test_refresh_after_merge_reads_the_alias_column(self) -> None:
        client = _client()
        client.select = AsyncMock(
            return_value=MagicMock(is_empty=False, first={"id": RecordId(table="u681_people", id="a"), "first-name": "Bob"})
        )
        person = _person()
        with _manager(client):
            await person.merge(first_name="Bob")
        assert person.first_name == "Bob"
