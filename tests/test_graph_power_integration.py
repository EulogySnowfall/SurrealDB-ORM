"""Integration tests for v0.36.0 Graph Power against a real server.

The graph (seeded before each test)::

    gp_person : a(A, active), b(B, active), c(C, inactive), d(D, active),
                e(E, active), z(Z, active, isolated), `7x`(Seven, no `active`)
    gp_knows  : a->b (w=1), b->c (w=1), c->d (w=1), a->e (w=5), e->d (w=1),
                d->a (w=1), d->`7x` (w=1)
    gp_employs: gp_company:acme -> b, acme -> c

Result order of a recursive idiom is unspecified, so results are compared as
sets of ids unless the order is what is tested.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterable
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.q import Q
from src.surreal_orm.types import TableType
from surreal_sdk.protocol.cbor import RecordId
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_graph_power"

pytestmark = pytest.mark.integration


class GPPerson(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="gp_person")

    id: str | None = None
    name: str = ""
    active: bool | None = None


class GPKnows(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="gp_knows", table_type=TableType.RELATION)

    id: str | None = None
    w: int = 0


class GPChain(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="gp_chain")

    id: str | None = None


class GPCompany(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="gp_company")

    id: str | None = None
    name: str = ""


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[None, Any]:
    """Point the ORM at a database of its own for this module."""
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


PEOPLE: dict[str, tuple[str, bool | None]] = {
    "a": ("A", True),
    "b": ("B", True),
    "c": ("C", False),
    "d": ("D", True),
    "e": ("E", True),
    "z": ("Z", True),
    "7x": ("Seven", None),
}

KNOWS: list[tuple[str, str, int]] = [
    ("a", "b", 1),
    ("b", "c", 1),
    ("c", "d", 1),
    ("a", "e", 5),
    ("e", "d", 1),
    ("d", "a", 1),
    ("d", "7x", 1),
]


@pytest.fixture(autouse=True)
async def graph() -> AsyncGenerator[dict[str, GPPerson], Any]:
    """Seed the graph of the module docstring; returns the people by id."""
    await GPPerson.raw_query("DELETE gp_person; DELETE gp_knows; DELETE gp_company; DELETE gp_employs;")
    people: dict[str, GPPerson] = {}
    for key, (name, active) in PEOPLE.items():
        person = GPPerson(id=key, name=name) if active is None else GPPerson(id=key, name=name, active=active)
        await person.save()
        people[key] = person
    for src, dst, w in KNOWS:
        await people[src].relate("gp_knows", people[dst], w=w)
    acme = GPCompany(id="acme", name="Acme")
    await acme.save()
    await acme.relate("gp_employs", people["b"])
    await acme.relate("gp_employs", people["c"])
    yield people


def ids(rows: Iterable[Any]) -> set[str]:
    return {str(row.get_id()) for row in rows}


def walk(source: Any, **kwargs: Any) -> Any:
    edge = kwargs.pop("edge", "gp_knows")
    return GPPerson.objects().reachable_from(source, edge, **kwargs)


class TestReachable:
    """I1."""

    async def test_basic(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk(graph["a"], max_depth=3).exec()
        assert ids(rows) == {"b", "c", "d", "e", "7x"}
        assert all(isinstance(row, GPPerson) for row in rows)

    async def test_include_source(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk(graph["a"], max_depth=3, include_source=True).exec()
        assert ids(rows) == {"a", "b", "c", "d", "e", "7x"}

    async def test_min_depth(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"], max_depth=3, min_depth=2).exec()) == {"c", "d", "7x"}

    async def test_unbounded(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"]).exec()) == {"b", "c", "d", "e", "7x"}

    async def test_edge_model(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"], edge=GPKnows, max_depth=1).exec()) == {"b", "e"}

    async def test_count(self, graph: dict[str, GPPerson]) -> None:
        assert await walk(graph["a"], max_depth=3).count() == 5
        assert await walk(graph["a"], max_depth=3, include_source=True).count() == 6
        assert await walk(graph["a"], max_depth=3).filter(active=True).count() == 3


class TestFilters:
    """I2: filter() filters the result, node_filter= / edge_filter= prune the walk."""

    async def test_edge_filter(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"], max_depth=3, edge_filter=Q(w__lt=5)).exec()) == {"b", "c", "d"}

    async def test_edge_filter_then_result_filter(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk(graph["a"], max_depth=3, edge_filter=Q(w__lt=5)).filter(active=True).exec()
        assert ids(rows) == {"b", "d"}

    async def test_edge_filter_and_node_filter(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk(graph["a"], max_depth=3, edge_filter=Q(w__lt=5), node_filter=Q(active=True)).exec()
        assert ids(rows) == {"b"}

    async def test_node_filter(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"], max_depth=3, node_filter=Q(active=True)).exec()) == {"b", "d", "e"}

    async def test_edge_in_instance(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk(graph["a"], max_depth=3, edge_filter=Q(**{"in": graph["a"]})).exec()
        assert ids(rows) == {"b", "e"}

    async def test_edge_out_string(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"], max_depth=3, edge_filter=Q(out="gp_person:b")).exec()) == {"b"}

    async def test_edge_out_in_instances(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk(graph["a"], max_depth=3, edge_filter=Q(out__in=[graph["b"], graph["e"]])).exec()
        assert ids(rows) == {"b", "e"}

    async def test_edge_out_bare_id_refused(self, graph: dict[str, GPPerson]) -> None:
        with pytest.raises(ValueError, match="edge_filter on 'out' compares records"):
            walk(graph["a"], max_depth=3, edge_filter=Q(out="b"))


class TestDirection:
    """I3."""

    async def test_in(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"], max_depth=3, direction="in").exec()) == {"b", "c", "d", "e"}

    async def test_both(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"], max_depth=2, direction="both").exec()) == {"b", "c", "d", "e", "7x"}


class TestChained:
    """I4 (without grouped counts)."""

    async def test_order_and_limit(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk(graph["a"], max_depth=3).order_by("name").limit(2).exec()
        assert [row.name for row in rows] == ["B", "C"]

    async def test_first(self, graph: dict[str, GPPerson]) -> None:
        row = await walk(graph["a"], max_depth=3).order_by("name").first()
        assert row.get_id() == "b"

    async def test_get(self, graph: dict[str, GPPerson]) -> None:
        qs = walk(graph["a"], max_depth=3)
        assert (await qs.get("d")).get_id() == "d"
        assert (await qs.get("gp_person:e")).get_id() == "e"

    async def test_get_not_reachable(self, graph: dict[str, GPPerson]) -> None:
        with pytest.raises(GPPerson.DoesNotExist):
            await walk(graph["a"], max_depth=3).get("z")

    async def test_all(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk(graph["a"], max_depth=3).all()) == {"b", "c", "d", "e", "7x"}

    async def test_select(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk(graph["a"], max_depth=3).select("name").exec()
        assert {row.name for row in rows} == {"B", "C", "D", "E", "Seven"}

    async def test_grouped_counts(self, graph: dict[str, GPPerson]) -> None:
        from src.surreal_orm.aggregations import Count

        rows = await walk(graph["a"], max_depth=3).values("active").annotate(n=Count()).exec()
        assert {row.get("active"): row["n"] for row in rows} == {True: 3, False: 1, None: 1}

    async def test_aggregates(self, graph: dict[str, GPPerson]) -> None:
        # Edge weights are not node fields: aggregate a node field instead.
        await GPPerson.raw_query("UPDATE gp_person SET score = 1; UPDATE gp_person:e SET score = 5;")
        qs = walk(graph["a"], max_depth=3)
        assert await qs.sum("score") == 9
        assert await walk(graph["a"], max_depth=3).min("score") == 1
        assert await walk(graph["a"], max_depth=3).max("score") == 5
        assert await walk(graph["a"], max_depth=1).avg("score") == 3.0


class TestOtherSourceTable:
    """I5 (first part)."""

    async def test_company_source(self, graph: dict[str, GPPerson]) -> None:
        rows = await walk("gp_company:acme", edge="gp_employs", max_depth=1).exec()
        assert ids(rows) == {"b", "c"}
        assert all(isinstance(row, GPPerson) for row in rows)

    async def test_company_instance_source(self, graph: dict[str, GPPerson]) -> None:
        acme = await GPCompany.objects().get("acme")
        assert ids(await walk(acme, edge="gp_employs", max_depth=1).exec()) == {"b", "c"}

    async def test_include_source_refused(self, graph: dict[str, GPPerson]) -> None:
        with pytest.raises(ValueError, match="include_source=True needs a source in 'gp_person'"):
            walk("gp_company:acme", edge="gp_employs", max_depth=1, include_source=True)


class TestKeys:
    """I6: escaped and integer keys."""

    async def test_backticked_key_in(self, graph: dict[str, GPPerson]) -> None:
        assert ids(await walk("gp_person:`7x`", max_depth=1, direction="in").exec()) == {"d"}

    async def test_backticked_key_out(self, graph: dict[str, GPPerson]) -> None:
        assert await walk("gp_person:`7x`", max_depth=1).exec() == []

    async def test_angle_key_refused(self, graph: dict[str, GPPerson]) -> None:
        with pytest.raises(ValueError, match="The ⟨…⟩ record-id spelling is not supported"):
            walk("gp_person:⟨7x⟩", max_depth=1)

    async def test_integer_key(self, graph: dict[str, GPPerson]) -> None:
        await GPPerson.raw_query(
            "CREATE gp_person:1 SET name = 'One', active = true; RELATE gp_person:1->gp_knows->gp_person:a SET w = 1;"
        )
        assert ids(await walk(RecordId("gp_person", 1), max_depth=1).exec()) == {"a"}


class TestRecursionLimit:
    """I9: past the server's recursion limit, a statement error is raised, never an empty result."""

    @pytest.fixture
    async def chain(self) -> AsyncGenerator[None, Any]:
        await GPChain.raw_query(
            "DELETE gp_chain; DELETE gp_link; "
            "FOR $i IN 0..301 { CREATE type::record('gp_chain', $i); }; "
            "FOR $i IN 0..300 { LET $a = type::record('gp_chain', $i); "
            "LET $b = type::record('gp_chain', $i + 1); RELATE $a->gp_link->$b; };"
        )
        yield
        await GPChain.raw_query("DELETE gp_chain; DELETE gp_link;")

    async def test_unbounded_raises(self, chain: None) -> None:
        from surreal_sdk.exceptions import QueryError

        qs = GPChain.objects().reachable_from(RecordId("gp_chain", 0), "gp_link")
        with pytest.raises(QueryError, match="recursion limit of 256"):
            await qs.exec()

    async def test_count_unbounded_raises(self, chain: None) -> None:
        from surreal_sdk.exceptions import QueryError

        qs = GPChain.objects().reachable_from(RecordId("gp_chain", 0), "gp_link")
        with pytest.raises(QueryError, match="recursion limit of 256"):
            await qs.count()

    async def test_max_depth_256(self, chain: None) -> None:
        qs = GPChain.objects().reachable_from(RecordId("gp_chain", 0), "gp_link", max_depth=256)
        assert len(await qs.exec()) == 256
        qs = GPChain.objects().reachable_from(RecordId("gp_chain", 0), "gp_link", max_depth=256)
        assert await qs.count() == 256


def path_ids(paths: Iterable[Iterable[Any]]) -> set[tuple[str, ...]]:
    return {tuple(str(node.get_id()) for node in path) for path in paths}


class TestPaths:
    """I7, I5 (rest) and the paths_from() part of I10."""

    async def test_paths(self, graph: dict[str, GPPerson]) -> None:
        paths = await GPPerson.objects().paths_from(graph["a"], "gp_knows", max_depth=2)
        assert path_ids(paths) == {("a", "b", "c"), ("a", "e", "d")}
        assert len(paths) == 2
        assert all(isinstance(node, GPPerson) for path in paths for node in path)

    async def test_paths_without_source(self, graph: dict[str, GPPerson]) -> None:
        paths = await GPPerson.objects().paths_from(graph["a"], "gp_knows", max_depth=2, include_source=False)
        assert path_ids(paths) == {("b", "c"), ("e", "d")}

    async def test_paths_min_depth(self, graph: dict[str, GPPerson]) -> None:
        paths = await GPPerson.objects().paths_from(graph["c"], "gp_knows", max_depth=3, min_depth=3)
        assert path_ids(paths) == {("c", "d", "a", "e"), ("c", "d", "a", "b")}

    async def test_other_source_table_refused(self, graph: dict[str, GPPerson]) -> None:
        with pytest.raises(ValueError, match="include_source=True needs a source in 'gp_person'"):
            await GPPerson.objects().paths_from("gp_company:acme", "gp_employs", max_depth=1)

    async def test_other_source_table(self, graph: dict[str, GPPerson]) -> None:
        paths = await GPPerson.objects().paths_from("gp_company:acme", "gp_employs", max_depth=1, include_source=False)
        assert path_ids(paths) == {("b",), ("c",)}

    async def test_query_logger(self, graph: dict[str, GPPerson]) -> None:
        from src.surreal_orm.debug import QueryLogger

        async with QueryLogger() as log:
            await GPPerson.objects().paths_from(graph["a"], "gp_knows", max_depth=2)
        assert [q.sql for q in log.queries] == [
            "LET $_g_paths = $_g_src.{1..2+path+inclusive}(->gp_knows->gp_person); "
            "RETURN { paths: $_g_paths, nodes: (SELECT * FROM array::distinct(array::flatten($_g_paths ?? []))) };"
        ]
        assert log.queries[0].variables == {"_g_src": RecordId("gp_person", "a")}
