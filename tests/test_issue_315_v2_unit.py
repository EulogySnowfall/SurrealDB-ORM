"""Unit tests for issue #315 (v2 line) — QueryCache keys and invalidation follow the connection.

A ``.cache()`` query run under ``using("b")`` must never be answered from an
entry stored under ``using("a")`` when the two connections point at different
databases, and a write through one connection must invalidate the entries of
the database it went to.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.surreal_orm.cache import QueryCache
from src.surreal_orm.connection_manager import SurrealDBConnectionManager
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.signals import post_delete, post_save, post_update


class I315UnitItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i315_unit_items")
    id: str | None = None
    label: str = ""


class I315UnitRouted(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i315_unit_routed", connection="i315_b")
    id: str | None = None


@pytest.fixture(autouse=True)
def _registry_and_cache() -> Iterator[None]:
    """Register two connections on two databases; restore everything afterwards."""
    mgr = SurrealDBConnectionManager
    saved_configs = dict(mgr._configs)
    saved_clients = dict(mgr._clients)
    saved_ws_clients = dict(mgr._ws_clients)
    for name, db in (("i315_a", "db_a"), ("i315_b", "db_b")):
        mgr.add_connection(
            name,
            url="http://localhost:8000",
            user="root",
            password="s3cret-pw",
            namespace="ns",
            database=db,
        )
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
    # add_connection("default") rewrites the legacy class vars: put them back.
    mgr._clear_legacy_vars()
    if "default" in saved_configs:
        mgr._sync_legacy_vars(saved_configs["default"])


def _ident(name: str) -> tuple[str, str | None, str | None, str | None]:
    config = SurrealDBConnectionManager.get_config(name)
    assert config is not None
    return (name, config.url, config.namespace, config.database)


class TestMakeKeyConnection:
    def test_differs_between_connection_identities(self) -> None:
        k_a = QueryCache.make_key("SELECT * FROM t", {"_f0": 1}, "t", connection=_ident("i315_a"))
        k_b = QueryCache.make_key("SELECT * FROM t", {"_f0": 1}, "t", connection=_ident("i315_b"))
        assert k_a != k_b

    def test_stable_for_the_same_identity(self) -> None:
        k1 = QueryCache.make_key("SELECT * FROM t", {"_f0": 1}, "t", connection=_ident("i315_a"))
        k2 = QueryCache.make_key("SELECT * FROM t", {"_f0": 1}, "t", connection=_ident("i315_a"))
        assert k1 == k2

    def test_old_call_shape_still_works(self) -> None:
        k1 = QueryCache.make_key("SELECT * FROM t", {}, "t")
        k2 = QueryCache.make_key("SELECT * FROM t", {}, "t")
        assert k1 == k2
        assert len(k1) == 64

    def test_reregistered_name_with_another_database_gets_a_new_key(self) -> None:
        before = QueryCache.make_key("q", {}, "t", connection=QueryCache._connection_identity(I315UnitItem))
        SurrealDBConnectionManager.add_connection(
            "default", url="http://localhost:8000", user="root", password="root", namespace="ns", database="db_x"
        )
        mid = QueryCache.make_key("q", {}, "t", connection=QueryCache._connection_identity(I315UnitItem))
        SurrealDBConnectionManager.add_connection(
            "default", url="http://localhost:8000", user="root", password="root", namespace="ns", database="db_y"
        )
        after = QueryCache.make_key("q", {}, "t", connection=QueryCache._connection_identity(I315UnitItem))
        assert len({before, mid, after}) == 3


class TestConnectionIdentity:
    async def test_follows_using_context(self) -> None:
        async with SurrealDBConnectionManager.using("i315_a"):
            ident_a = QueryCache._connection_identity(I315UnitItem)
        async with SurrealDBConnectionManager.using("i315_b"):
            ident_b = QueryCache._connection_identity(I315UnitItem)
        assert ident_a == ("i315_a", "http://localhost:8000", "ns", "db_a")
        assert ident_b == ("i315_b", "http://localhost:8000", "ns", "db_b")

    def test_follows_model_config_connection(self) -> None:
        assert QueryCache._connection_identity(I315UnitRouted)[0] == "i315_b"

    def test_never_contains_credentials(self) -> None:
        ident = QueryCache._connection_identity(I315UnitRouted)
        assert "s3cret-pw" not in ident
        assert "root" not in ident

    def test_unregistered_name_resolves_without_config(self) -> None:
        SurrealDBConnectionManager._configs.pop("default", None)
        assert QueryCache._connection_identity(I315UnitItem) == ("default", None, None, None)


class TestPerConnectionInvalidation:
    def _fill(self) -> tuple[str, str]:
        ia, ib = _ident("i315_a"), _ident("i315_b")
        k_a = QueryCache.make_key("q", {}, "i315_unit_items", connection=ia)
        k_b = QueryCache.make_key("q", {}, "i315_unit_items", connection=ib)
        QueryCache.set(k_a, ["from-A"], "i315_unit_items", connection=ia)
        QueryCache.set(k_b, ["from-B"], "i315_unit_items", connection=ib)
        return k_a, k_b

    @pytest.mark.parametrize("signal", [post_save, post_update, post_delete])
    async def test_signal_invalidates_only_the_active_connection(self, signal: object) -> None:
        k_a, k_b = self._fill()
        async with SurrealDBConnectionManager.using("i315_a"):
            await signal.send(sender=I315UnitItem, instance=I315UnitItem(id="r1"))  # type: ignore[attr-defined]
        assert QueryCache.get(k_a) is None
        assert QueryCache.get(k_b) == ["from-B"]

    async def test_signal_invalidates_every_name_on_the_same_database(self) -> None:
        SurrealDBConnectionManager.add_connection(
            "i315_a_alias", url="http://localhost:8000", user="other", password="pw", namespace="ns", database="db_a"
        )
        k_a, k_b = self._fill()
        ialias = _ident("i315_a_alias")
        k_alias = QueryCache.make_key("q", {}, "i315_unit_items", connection=ialias)
        assert k_alias != k_a
        QueryCache.set(k_alias, ["from-A"], "i315_unit_items", connection=ialias)
        async with SurrealDBConnectionManager.using("i315_a"):
            await post_save.send(sender=I315UnitItem, instance=I315UnitItem(id="r1"))
        assert QueryCache.get(k_a) is None
        assert QueryCache.get(k_alias) is None
        assert QueryCache.get(k_b) == ["from-B"]

    @pytest.mark.parametrize("signal", [post_save, post_update, post_delete])
    async def test_signal_invalidates_entries_set_without_connection(self, signal: object) -> None:
        # A direct QueryCache.set() without connection= carries no database:
        # any write to the table must still drop it, as before the per-connection key.
        k_a, k_b = self._fill()
        QueryCache.set("no-conn", ["unknown-db"], "i315_unit_items")
        QueryCache.set("no-conn-other", "x", "orders")
        async with SurrealDBConnectionManager.using("i315_a"):
            await signal.send(sender=I315UnitItem, instance=I315UnitItem(id="r1"))  # type: ignore[attr-defined]
        assert QueryCache.get("no-conn") is None
        assert QueryCache.get(k_a) is None
        assert QueryCache.get(k_b) == ["from-B"]
        assert QueryCache.get("no-conn-other") == "x"

    def test_public_invalidate_clears_the_table_for_every_connection(self) -> None:
        k_a, k_b = self._fill()
        QueryCache.set("other", "x", "orders")
        assert QueryCache.invalidate(I315UnitItem) == 2
        assert QueryCache.get(k_a) is None
        assert QueryCache.get(k_b) is None
        assert QueryCache.get("other") == "x"

    def test_stats_counts_tables_not_buckets(self) -> None:
        self._fill()
        assert QueryCache.stats()["entries"] == 2
        assert QueryCache.stats()["tables"] == 1

    def test_fifo_eviction_keeps_indexes_consistent(self) -> None:
        QueryCache._max_size = 2
        k_a, k_b = self._fill()
        ia = _ident("i315_a")
        k_c = QueryCache.make_key("q2", {}, "i315_unit_items", connection=ia)
        QueryCache.set(k_c, ["c"], "i315_unit_items", connection=ia)
        assert QueryCache.get(k_a) is None
        assert set(QueryCache._cache) == {k_b, k_c}
        indexed = set().union(*QueryCache._table_keys.values())
        assert indexed == {k_b, k_c}
        QueryCache.invalidate(I315UnitItem)
        assert QueryCache._cache == {}
        assert QueryCache._table_keys == {}
