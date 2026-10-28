r"""Integration tests for #296: record IDs with special characters round-trip.

Every ORM path that writes a record ID into SurrealQL text goes through
``format_thing()`` / ``escape_record_id()``. Before the fix, an ID with a
backtick was emitted with the backtick doubled, which SurrealDB does not read
as an escape, and a backslash was emitted as-is, which SurrealDB reads as one.
Such IDs saved and loaded fine (those paths bind a ``RecordId``) but every
query-string path — ``merge()`` with a ``SurrealFunc``, the atomic array
helpers, relations, traversals — failed or addressed something else.

For each ID the test drives every such path against a record with that ID and
checks two things: the target record is the one that changed, and nothing
else in the database did — the table list (``INFO FOR DB``) and every other
row are identical before and after.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager, SurrealFunc
from src.surreal_orm.utils import format_thing
from surreal_sdk.protocol.cbor import RecordId
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_296"

NODES = "i296_nodes"
LINKS = "i296_links"
OTHERS = "i296_others"

#: IDs that need escaping; the last ones combine the characters.
SPECIAL_IDS = [
    "a`b",
    "a\\b",
    "a\\`b",
    "trail\\",
    "with space",
    "semi;colon",
    "it's",
    'say "hi"',
    "dash--dash",
    "7abc",
    "x\\`; --y\\",
    "`\\`\\",
]


class I296Node(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=NODES)

    id: str | None = None
    name: str = ""
    tags: list[str] | None = None
    stamp: Any = None


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


@pytest.fixture(autouse=True)
async def seeded() -> AsyncGenerator[None, Any]:
    """Fresh tables, with rows the target record must never affect."""
    client = await SurrealDBConnectionManager.get_client()
    drop = f"REMOVE TABLE IF EXISTS {NODES}; REMOVE TABLE IF EXISTS {LINKS}; REMOVE TABLE IF EXISTS {OTHERS};"
    await client.query(drop)
    response = await client.query(
        f"CREATE {NODES}:peer SET name = 'peer', tags = ['p'];"
        f"CREATE {NODES}:bystander SET name = 'bystander', tags = ['a', 'b'];"
        f"CREATE {OTHERS}:one SET v = 1; CREATE {OTHERS}:two SET v = 2;"
        f"RELATE {NODES}:peer->{LINKS}->{NODES}:bystander SET kind = 'kept';"
    )
    assert response.is_ok, response.raw
    yield
    await client.query(drop)


async def _query(sql: str, variables: dict[str, Any] | None = None) -> list[Any]:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(sql, variables or {})
    assert response.is_ok, response.raw
    return list(response.all_records or [])


async def _snapshot() -> tuple[list[str], dict[str, list[Any]]]:
    """The table list and every row of every table."""
    client = await SurrealDBConnectionManager.get_client()
    info = await client.query("INFO FOR DB;")
    assert info.is_ok, info.raw
    tables = sorted(info.first_result.result["tables"].keys())  # type: ignore[union-attr]
    rows = {table: await _query(f"SELECT * FROM {table} ORDER BY id;") for table in tables}
    return tables, rows


async def _target_ids() -> list[str]:
    return [str(v) for v in await _query(f"SELECT VALUE record::id(id) FROM {NODES} WHERE name = 'target';")]


@pytest.mark.integration
@pytest.mark.parametrize("rid", SPECIAL_IDS)
async def test_record_ids_with_special_characters_round_trip(rid: str) -> None:
    before = await _snapshot()
    peer = await I296Node.objects().get("peer")

    # save / get / record::id()
    node = I296Node(id=rid, name="target", tags=[])
    await node.save()
    loaded = await I296Node.objects().get(rid)
    assert loaded.get_id() == rid
    assert await _target_ids() == [rid]

    # merge() with a SurrealFunc goes through an UPDATE statement
    await loaded.merge(stamp=SurrealFunc("time::now()"))
    assert loaded.stamp is not None
    stamped = await _query(f"SELECT VALUE record::id(id) FROM {NODES} WHERE stamp != NONE;")
    assert stamped == [rid]

    # atomic array helpers, by bare ID and by full "table:id"
    await I296Node.atomic_append(rid, "tags", "x")
    await I296Node.atomic_append(format_thing(NODES, rid), "tags", "x")
    await I296Node.atomic_set_add(rid, "tags", "y")
    await I296Node.atomic_set_add(rid, "tags", "y")
    await I296Node.atomic_remove(rid, "tags", "x")
    tags = await _query(f"SELECT VALUE tags FROM {NODES} WHERE name = 'target';")
    assert tags == [["y"]]

    # relate() in both directions, then read them back
    await loaded.relate(LINKS, peer)
    await peer.relate(LINKS, loaded)
    sources = await _query(
        f"SELECT VALUE record::id(in) FROM {LINKS} WHERE out = $peer;", {"peer": RecordId(NODES, "peer")}
    )
    assert sources == [rid]

    out_related = await loaded.get_related(LINKS, direction="out", model_class=I296Node)
    assert [n.get_id() for n in out_related] == ["peer"]
    in_related = await loaded.get_related(LINKS, direction="in", model_class=I296Node)
    assert [n.get_id() for n in in_related] == ["peer"]
    to_target = await peer.get_related(LINKS, direction="out", model_class=I296Node)
    assert sorted(str(n.get_id()) for n in to_target) == sorted(["bystander", rid])

    # graph traversal from the record, both directions
    outgoing = await I296Node.objects().filter(id=rid).graph_query(f"->{LINKS}->{NODES}")
    assert [r.get("name") for r in outgoing] == ["peer"]
    incoming = await I296Node.objects().filter(id=rid).graph_query(f"<-{LINKS}<-{NODES}")
    assert [r.get("name") for r in incoming] == ["peer"]
    traversed = await _query(f"SELECT VALUE name FROM {format_thing(NODES, rid)}->{LINKS}->{NODES};")
    assert traversed == ["peer"]

    # remove_relation() by instance, then by "table:id" string
    await loaded.remove_relation(LINKS, peer)
    assert await _query(f"SELECT VALUE record::id(out) FROM {LINKS} WHERE in = $t;", {"t": RecordId(NODES, rid)}) == []
    await peer.remove_relation(LINKS, format_thing(NODES, rid))
    assert await _query(f"SELECT VALUE record::id(in) FROM {LINKS} WHERE out = $t;", {"t": RecordId(NODES, rid)}) == []

    # remove_all_relations() removes only the target's edges
    await loaded.relate(LINKS, peer)
    await peer.relate(LINKS, loaded)
    await loaded.remove_all_relations(LINKS, direction="both")
    remaining = await _query(f"SELECT VALUE kind FROM {LINKS};")
    assert remaining == ["kept"]

    # delete()
    await loaded.delete()
    assert await _target_ids() == []

    assert await _snapshot() == before
