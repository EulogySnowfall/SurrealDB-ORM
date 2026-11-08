"""
Unit tests for #465: relation writes drop the edge / through table's cached entries.

``relate()``, ``remove_relation()``, ``remove_all_relations()`` and the
``RelationManager`` writes go straight to the client (or the transaction) and
fire no model signal, so before the fix a ``.cache()`` read of a model mapped to
the edge table kept serving the rows from before the write.
"""

from __future__ import annotations

import gc
import weakref
from collections.abc import Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.cache import QueryCache
from src.surreal_orm.connection_manager import SurrealDBConnectionManager
from src.surreal_orm.fields.relation import RelationInfo
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, SurrealDbError
from src.surreal_orm.relations import RelationManager
from surreal_sdk.exceptions import TransactionError
from surreal_sdk.transaction import BaseTransaction, HTTPTransaction, WebSocketTransaction
from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

_EDGE = "i465_follows"
_EDGE_2 = "i465_likes"
_THROUGH = "i465_memberships"
_OTHER = "i465_others"


class I465User(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i465_users")
    id: str | None = None
    name: str = ""


class I465Group(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i465_groups")
    id: str | None = None


def _ok() -> QueryResponse:
    return QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=[])])


def _client() -> AsyncMock:
    client = AsyncMock()
    client.query = AsyncMock(return_value=_ok())
    relate_result = MagicMock()
    relate_result.exists = True
    relate_result.record = {"id": f"{_EDGE}:e1"}
    client.relate = AsyncMock(return_value=relate_result)
    return client


class _Tx:
    """Stands in for a transaction: ``relate()`` and ``query()`` queue a statement."""

    def __init__(self) -> None:
        self.relate = AsyncMock(return_value=None)
        self.query = AsyncMock(return_value=_ok())


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:
    QueryCache.clear()
    QueryCache._enabled = True
    yield
    QueryCache.clear()


def _fill(*tables: str) -> None:
    """One entry per table with a connection identity, one without, and one unrelated table."""
    for table in tables:
        QueryCache.set(f"{table}-conn", ["stale"], table, connection=("default", "http://h:8000", "ns", "db"))
        QueryCache.set(f"{table}-bare", ["stale"], table)
    QueryCache.set("other", ["kept"], _OTHER)


def _assert_dropped(*tables: str) -> None:
    for table in tables:
        assert QueryCache.get(f"{table}-conn") is None, table
        assert QueryCache.get(f"{table}-bare") is None, table
    assert QueryCache.get("other") == ["kept"]


def _alice() -> I465User:
    return I465User(id="alice", name="Alice")


def _bob() -> I465User:
    return I465User(id="bob", name="Bob")


# ── BaseSurrealModel relation methods ───────────────────────────────────────

_MODEL_WRITES: dict[str, tuple[Callable[[Any], Awaitable[Any]], tuple[str, ...]]] = {
    "relate": (lambda tx: _alice().relate(_EDGE, _bob(), tx=tx), (_EDGE,)),
    "relate_reverse": (lambda tx: _alice().relate(_EDGE, _bob(), reverse=True, tx=tx), (_EDGE,)),
    "relate_edge_data": (lambda tx: _alice().relate(_EDGE, _bob(), tx=tx, since="2026"), (_EDGE,)),
    "remove_relation_instance": (lambda tx: _alice().remove_relation(_EDGE, _bob(), tx=tx), (_EDGE,)),
    "remove_relation_full_id": (lambda tx: _alice().remove_relation(_EDGE, "i465_users:bob", tx=tx), (_EDGE,)),
    "remove_relation_bare_id": (lambda tx: _alice().remove_relation(_EDGE, "bob", tx=tx), (_EDGE,)),
    "remove_relation_bare_id_reverse": (
        lambda tx: _alice().remove_relation(_EDGE, "bob", reverse=True, tx=tx),
        (_EDGE,),
    ),
    "remove_all_relations_single": (lambda tx: _alice().remove_all_relations(_EDGE, tx=tx), (_EDGE,)),
    "remove_all_relations_in": (lambda tx: _alice().remove_all_relations(_EDGE, direction="in", tx=tx), (_EDGE,)),
    "remove_all_relations_both": (
        lambda tx: _alice().remove_all_relations(_EDGE, direction="both", tx=tx),
        (_EDGE,),
    ),
    "remove_all_relations_list": (
        lambda tx: _alice().remove_all_relations([_EDGE, _EDGE_2], tx=tx),
        (_EDGE, _EDGE_2),
    ),
}


class TestModelRelationWritesInvalidate:
    """``relate()`` / ``remove_relation()`` / ``remove_all_relations()``."""

    @pytest.mark.parametrize("write", list(_MODEL_WRITES))
    async def test_direct_write_drops_the_edge_table(self, write: str) -> None:
        call, tables = _MODEL_WRITES[write]
        _fill(*tables)
        client = _client()
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
            await call(None)
        assert client.relate.await_count + client.query.await_count >= 1
        _assert_dropped(*tables)

    @pytest.mark.parametrize("write", list(_MODEL_WRITES))
    async def test_tx_write_drops_the_edge_table_when_queued(self, write: str) -> None:
        call, tables = _MODEL_WRITES[write]
        _fill(*tables)
        tx = _Tx()
        get_client = AsyncMock(side_effect=AssertionError("a tx= write must not use the connection"))
        with patch.object(SurrealDBConnectionManager, "get_client", get_client):
            await call(tx)
        assert tx.relate.await_count + tx.query.await_count >= 1
        _assert_dropped(*tables)

    async def test_remove_all_relations_drops_every_table_when_a_later_delete_fails(self) -> None:
        """The first DELETE reached the server; the second one failing must not keep stale rows."""
        _fill(_EDGE, _EDGE_2)
        client = _client()
        client.query = AsyncMock(side_effect=[_ok(), ConnectionError("connection lost")])
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)),
            pytest.raises(ConnectionError, match="connection lost"),
        ):
            await _alice().remove_all_relations([_EDGE, _EDGE_2])
        _assert_dropped(_EDGE, _EDGE_2)

    @pytest.mark.parametrize(
        "call",
        [
            lambda: I465User(name="unsaved").relate(_EDGE, _bob()),
            lambda: _alice().relate(_EDGE, I465User(name="unsaved")),
            lambda: _alice().relate("bad name", _bob()),
            lambda: I465User(name="unsaved").remove_relation(_EDGE, _bob()),
            lambda: _alice().remove_relation(_EDGE, I465User(name="unsaved")),
            lambda: I465User(name="unsaved").remove_all_relations(_EDGE),
            lambda: _alice().remove_all_relations(_EDGE, direction="sideways"),  # type: ignore[arg-type]
        ],
    )
    async def test_write_refused_before_the_server_sends_nothing(self, call: Callable[[], Awaitable[Any]]) -> None:
        client = _client()
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)),
            pytest.raises((SurrealDbError, ValueError)),
        ):
            await call()
        client.relate.assert_not_awaited()
        client.query.assert_not_awaited()


# ── RelationManager ─────────────────────────────────────────────────────────


def _graph_manager(reverse: bool = False) -> RelationManager:
    info = RelationInfo(to_model="I465User", relation_type="relation", edge_table=_EDGE, reverse=reverse)
    return RelationManager(_alice(), info, "following")


def _m2m_manager(through: str | None = _THROUGH) -> RelationManager:
    info = RelationInfo(to_model="I465Group", relation_type="many_to_many", through=through)
    return RelationManager(_alice(), info, "groups")


_MANAGER_WRITES: dict[str, tuple[Callable[[], Awaitable[Any]], tuple[str, ...]]] = {
    "graph_add": (lambda: _graph_manager().add(_bob()), (_EDGE,)),
    "graph_add_reverse": (lambda: _graph_manager(reverse=True).add(_bob(), since="2026"), (_EDGE,)),
    "graph_remove": (lambda: _graph_manager().remove(_bob()), (_EDGE,)),
    "graph_remove_reverse": (lambda: _graph_manager(reverse=True).remove(_bob()), (_EDGE,)),
    "graph_set": (lambda: _graph_manager().set([_bob()]), (_EDGE,)),
    "graph_set_empty": (lambda: _graph_manager().set([]), (_EDGE,)),
    "graph_clear": (lambda: _graph_manager().clear(), (_EDGE,)),
    "graph_clear_reverse": (lambda: _graph_manager(reverse=True).clear(), (_EDGE,)),
    "m2m_add": (lambda: _m2m_manager().add(I465Group(id="g1")), (_THROUGH,)),
    "m2m_remove": (lambda: _m2m_manager().remove(I465Group(id="g1")), (_THROUGH,)),
    "m2m_set": (lambda: _m2m_manager().set([I465Group(id="g1")]), (_THROUGH,)),
    "m2m_clear": (lambda: _m2m_manager().clear(), (_THROUGH,)),
    # No ``through``: the table actually written is named after the two tables.
    "m2m_add_default_through": (lambda: _m2m_manager(None).add(I465Group(id="g1")), ("i465_users_i465_groups",)),
    "m2m_remove_default_through": (
        lambda: _m2m_manager(None).remove(I465Group(id="g1")),
        ("i465_users_i465_groups",),
    ),
    "m2m_clear_default_through": (lambda: _m2m_manager(None).clear(), ("i465_users_",)),
}


class TestRelationManagerWritesInvalidate:
    """``RelationManager.add()`` / ``remove()`` / ``set()`` / ``clear()``."""

    @pytest.mark.parametrize("write", list(_MANAGER_WRITES))
    async def test_write_drops_the_table_written(self, write: str) -> None:
        call, tables = _MANAGER_WRITES[write]
        _fill(*tables)
        client = _client()
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
            await call()
        _assert_dropped(*tables)

    async def test_add_drops_the_edge_table_when_a_later_object_is_unsaved(self) -> None:
        """The first edge was written before the unsaved object raised."""
        _fill(_EDGE)
        client = _client()
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)),
            pytest.raises(ValueError, match="unsaved"),
        ):
            await _graph_manager().add(_bob(), I465User(name="unsaved"))
        client.relate.assert_awaited_once()
        _assert_dropped(_EDGE)

    async def test_add_with_nothing_to_add_keeps_the_entries(self) -> None:
        _fill(_EDGE)
        client = _client()
        with patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)):
            await _graph_manager().add()
        assert QueryCache.get(f"{_EDGE}-bare") == ["stale"]

    @pytest.mark.parametrize("method", ["add", "remove", "clear"])
    async def test_unsaved_owner_sends_nothing(self, method: str) -> None:
        info = RelationInfo(to_model="I465User", relation_type="relation", edge_table=_EDGE)
        manager = RelationManager(I465User(name="unsaved"), info, "following")
        client = _client()
        with (
            patch.object(SurrealDBConnectionManager, "get_client", AsyncMock(return_value=client)),
            pytest.raises(ValueError, match="unsaved"),
        ):
            await getattr(manager, method)()
        client.relate.assert_not_awaited()
        client.query.assert_not_awaited()


# ── QueryCache entry point keyed by table ───────────────────────────────────


class TestInvalidateByTable:
    def test_drops_every_bucket_of_the_table_only(self) -> None:
        _fill(_EDGE)
        assert QueryCache._invalidate_table(_EDGE) == 2
        _assert_dropped(_EDGE)

    def test_invalidate_model_still_counts_its_entries(self) -> None:
        _fill("i465_users")
        assert QueryCache.invalidate(I465User) == 2
        _assert_dropped("i465_users")


# ── tx=: dropped again once the transaction commits ─────────────────────────


class I465Follows(BaseSurrealModel):
    """A model mapped to the edge table, written with ``save(tx=)`` beside a relation write."""

    model_config = SurrealConfigDict(table_name=_EDGE)
    id: str | None = None


def _tx_connection() -> MagicMock:
    """What a real transaction talks to: every statement and the commit are accepted."""
    conn = MagicMock()
    conn.query = AsyncMock(return_value=_ok())
    relate_result = MagicMock()
    relate_result.exists = True
    relate_result.record = {"id": f"{_EDGE}:e1"}
    conn.relate = AsyncMock(return_value=relate_result)
    conn.upsert = AsyncMock(return_value=MagicMock(records=[{"id": f"{_EDGE}:f1"}], is_empty=False))
    return conn


async def _open(kind: str) -> BaseTransaction:
    tx: BaseTransaction = (HTTPTransaction if kind == "http" else WebSocketTransaction)(_tx_connection())
    await tx._begin()
    return tx


def _no_client() -> AsyncMock:
    return AsyncMock(side_effect=AssertionError("a tx= write must not use the connection"))


class TestTxRelationWritesInvalidateAgainOnCommit:
    """A read cached between a ``tx=`` relation write and the commit does not outlive the commit."""

    @pytest.mark.parametrize("kind", ["http", "websocket"])
    @pytest.mark.parametrize("write", list(_MODEL_WRITES))
    async def test_entries_cached_before_the_commit_are_dropped_at_commit(self, write: str, kind: str) -> None:
        call, tables = _MODEL_WRITES[write]
        tx = await _open(kind)
        with patch.object(SurrealDBConnectionManager, "get_client", _no_client()):
            await call(tx)
        _fill(*tables)  # another task read the edge table before the commit
        await tx.commit()
        assert tx.is_committed
        _assert_dropped(*tables)

    @pytest.mark.parametrize("kind", ["http", "websocket"])
    @pytest.mark.parametrize("write", list(_MODEL_WRITES))
    async def test_rollback_drops_nothing_more(self, write: str, kind: str) -> None:
        call, tables = _MODEL_WRITES[write]
        tx = await _open(kind)
        with patch.object(SurrealDBConnectionManager, "get_client", _no_client()):
            await call(tx)
        _fill(*tables)
        await tx.rollback()
        assert tx._commit_callbacks == {}
        for table in tables:
            assert QueryCache.get(f"{table}-bare") == ["stale"], table
            assert QueryCache.get(f"{table}-conn") == ["stale"], table

    async def test_one_callback_per_table_and_transaction(self) -> None:
        """Keyed like the model writes (#360): a ``save(tx=)`` on the edge model shares the key."""
        QueryCache._connect_signals()
        tx = await _open("http")
        with patch.object(SurrealDBConnectionManager, "get_client", _no_client()):
            await _alice().relate(_EDGE, _bob(), tx=tx)
            await _alice().relate(_EDGE, _bob(), tx=tx)
            await _alice().remove_all_relations([_EDGE, _EDGE_2], tx=tx)
            await I465Follows(id="f1").save(tx=tx)
        assert set(tx._commit_callbacks) == {(QueryCache._COMMIT_KEY, _EDGE), (QueryCache._COMMIT_KEY, _EDGE_2)}
        await tx.rollback()

    async def test_callback_keeps_neither_transaction_nor_instance(self) -> None:
        tx = await _open("http")
        alice = _alice()
        with patch.object(SurrealDBConnectionManager, "get_client", _no_client()):
            await alice.relate(_EDGE, _bob(), tx=tx)
        tx_ref, alice_ref = weakref.ref(tx), weakref.ref(alice)
        callbacks = list(tx._commit_callbacks.values())
        del tx, alice
        gc.collect()
        assert callbacks
        assert tx_ref() is None
        assert alice_ref() is None

    async def test_failed_commit_drops_nothing_more(self) -> None:
        tx = await _open("http")
        with patch.object(SurrealDBConnectionManager, "get_client", _no_client()):
            await _alice().relate(_EDGE, _bob(), tx=tx)
        _fill(_EDGE)
        tx._connection.query = AsyncMock(side_effect=ConnectionError("connection lost"))
        with pytest.raises(TransactionError):
            await tx.commit()
        assert QueryCache.get(f"{_EDGE}-bare") == ["stale"]
