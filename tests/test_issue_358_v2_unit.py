"""Unit tests for issue #358 (v2 line) — a write invalidates QueryCache entries for every connection.

Reads stay keyed per connection, but the location a cached entry was stored
under is the raw ``(url, namespace, database)`` of its connection: one
database reached through two URL spellings is two buckets, and a
``save(tx=...)`` runs on the transaction's client whatever the model's
routing says.  A write must therefore drop the written table's entries in
every bucket, never only the writer's.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from src.surreal_orm.cache import QueryCache
from src.surreal_orm.connection_manager import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.signals import Signal, post_delete, post_save, post_update

_TABLE = "i358_unit_items"


class I358UnitItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=_TABLE)
    id: str | None = None
    label: str = ""


class I358UnitRouted(BaseSurrealModel):
    """Same table, routed to a connection on another database."""

    model_config = SurrealConfigDict(table_name=_TABLE, connection="i358_other")
    id: str | None = None
    label: str = ""


# One database under three spellings of its URL, plus another database.
_CONNECTIONS = {
    "i358_plain": ("http://localhost:8000", "db"),
    "i358_slash": ("http://localhost:8000/", "db"),
    "i358_ws": ("ws://127.0.0.1:8000", "db"),
    "i358_other": ("http://localhost:8000", "db_other"),
}


class _FakeDeferredTx:
    """Minimal deferred transaction: records the statements, returns nothing."""

    defers_results = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def upsert(self, thing: str, data: dict[str, Any]) -> None:
        self.calls.append(("upsert", thing))

    async def merge(self, thing: str, data: dict[str, Any]) -> None:
        self.calls.append(("merge", thing))

    async def create(self, table: str, data: dict[str, Any]) -> None:
        self.calls.append(("create", table))


@pytest.fixture(autouse=True)
def _registry_and_cache() -> Iterator[None]:
    mgr = SurrealDBConnectionManager
    saved_configs = dict(mgr._configs)
    saved_clients = dict(mgr._clients)
    saved_ws_clients = dict(mgr._ws_clients)
    for name, (url, database) in _CONNECTIONS.items():
        mgr.add_connection(name, url=url, user="root", password="root", namespace="ns", database=database)
    QueryCache.clear()
    QueryCache._enabled = True
    QueryCache._max_size = 1000
    QueryCache._default_ttl = 60
    QueryCache._connect_signals()
    yield
    QueryCache.clear()
    mgr._configs.clear()
    mgr._configs.update(saved_configs)
    mgr._clients.clear()
    mgr._clients.update(saved_clients)
    mgr._ws_clients.clear()
    mgr._ws_clients.update(saved_ws_clients)
    mgr._clear_legacy_vars()
    if "default" in saved_configs:
        mgr._sync_legacy_vars(saved_configs["default"])


def _cache_read(connection: str, data: Any) -> str:
    """Store *data* as a ``.cache()`` read of the table through *connection*."""
    ident = _identity(connection)
    key = QueryCache.make_key("SELECT * FROM i358_unit_items", {}, _TABLE, connection=ident)
    QueryCache.set(key, data, _TABLE, connection=ident)
    return key


def _identity(connection: str) -> tuple[str, str | None, str | None, str | None]:
    config = SurrealDBConnectionManager.get_config(connection)
    assert config is not None
    return (connection, config.url, config.namespace, config.database)


class TestWriteInvalidatesEverySpelling:
    @pytest.mark.parametrize("signal", [post_save, post_update, post_delete], ids=lambda s: s.name)
    async def test_write_through_one_spelling_drops_the_others_entry(self, signal: Signal) -> None:
        k_slash = _cache_read("i358_slash", ["one"])
        k_ws = _cache_read("i358_ws", ["one"])
        async with SurrealDBConnectionManager.using("i358_plain"):
            await signal.send(sender=I358UnitItem, instance=I358UnitItem(id="r2"))
        assert QueryCache.get(k_slash) is None
        assert QueryCache.get(k_ws) is None

    async def test_other_tables_are_left_alone(self) -> None:
        ident = _identity("i358_slash")
        QueryCache.set("other-table", "x", "i358_unrelated", connection=ident)
        async with SurrealDBConnectionManager.using("i358_plain"):
            await post_save.send(sender=I358UnitItem, instance=I358UnitItem(id="r2"))
        assert QueryCache.get("other-table") == "x"


class TestTransactionWriteInvalidatesWhateverTheRouting:
    async def test_save_tx_drops_the_entry_of_the_transactions_connection(self) -> None:
        # The model routes to i358_other; the transaction runs on i358_plain.
        k_plain = _cache_read("i358_plain", ["one"])
        tx = _FakeDeferredTx()
        await I358UnitRouted(id="r3", label="three").save(tx=tx)  # type: ignore[arg-type]
        assert tx.calls == [("upsert", "i358_unit_items:r3")]
        assert QueryCache.get(k_plain) is None


class TestReadsStayIsolated:
    def test_two_databases_never_share_a_key(self) -> None:
        k_plain = _cache_read("i358_plain", ["from-db"])
        k_other = _cache_read("i358_other", ["from-db-other"])
        assert k_plain != k_other
        assert QueryCache.get(k_plain) == ["from-db"]
        assert QueryCache.get(k_other) == ["from-db-other"]
