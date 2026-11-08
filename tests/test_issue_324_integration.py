r"""Integration tests for #324: one string names one record on every SDK path.

The RPC methods of both connections used to build their ``RecordId`` with a
reading of their own: ``t:1`` became the string ID ``"1"`` and ``t:⟨a b⟩``
kept its brackets, while ``HTTPTransaction`` (and SurrealQL itself) read the
integer ID 1 and the string ID ``"a b"``.

Each reference here is first created by inline SurrealQL, the server's own
reading, then updated, merged, selected and deleted through each of the four
paths: ``HTTPTransaction``, ``WebSocketTransaction`` and the RPC methods of
``HTTPConnection`` and ``WebSocketConnection``. Each must touch that record
and only it.

The ORM half saves records through the model API, including a string ID made
of digits, which the ORM escapes so that it never becomes an integer ID, and
drives ``merge(tx=)`` / ``save(tx=)`` on a saved record in an HTTP transaction.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.protocol.cbor import RecordId
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER, SURREALDB_WS_URL

pytestmark = pytest.mark.integration

SURREALDB_DATABASE = "test_issue_324"

NODES = "i324_nodes"
ITEMS = "i324_items"
#: A table name that is not an identifier: ``format_thing()`` does not escape it.
PROFILES = "i324-profiles"

#: (reference, ID the server reads from it inline).
REFERENCES: dict[str, int | str] = {
    f"{NODES}:1": 1,
    f"{NODES}:-5": -5,
    f"{NODES}:9223372036854775807": 2**63 - 1,
    f"{NODES}:9223372036854775808": "9223372036854775808",
    # Leading zeros: the server drops them inline, and so does the SDK.
    f"{NODES}:05": 5,
    f"{NODES}:-05": -5,
    f"{NODES}:09223372036854775808": "09223372036854775808",
    f"{NODES}:⟨a b⟩": "a b",
    f"{NODES}:`a b`": "a b",
    f"{NODES}:`1`": "1",
    f"{NODES}:plain_id": "plain_id",
}

Conn = HTTPConnection | WebSocketConnection


class I324Profile(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=PROFILES)

    id: str | None = None
    amount: int | None = None


class I324Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name=ITEMS)

    id: str | None = None
    amount: int | None = None
    label: str | None = None


async def _prepare(conn: Conn) -> Conn:
    await conn.connect()
    await conn.signin(SURREALDB_USER, SURREALDB_PASS)
    await conn.query(
        f"DEFINE NAMESPACE IF NOT EXISTS `{SURREALDB_NAMESPACE}`; DEFINE DATABASE IF NOT EXISTS `{SURREALDB_DATABASE}`;"
    )
    return conn


_DROP = f"REMOVE TABLE IF EXISTS {NODES}; REMOVE TABLE IF EXISTS {ITEMS}; REMOVE TABLE IF EXISTS `{PROFILES}`;"


@pytest.fixture
async def http() -> AsyncGenerator[HTTPConnection, None]:
    conn = await _prepare(HTTPConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE))
    assert isinstance(conn, HTTPConnection)
    await conn.query(_DROP)
    yield conn
    await conn.query(_DROP)
    await conn.close()


@pytest.fixture
async def ws() -> AsyncGenerator[WebSocketConnection, None]:
    conn = await _prepare(WebSocketConnection(SURREALDB_WS_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, auto_reconnect=False))
    assert isinstance(conn, WebSocketConnection)
    yield conn
    await conn.close()


async def _rows(conn: Conn) -> list[dict[str, Any]]:
    return sorted((await conn.query(f"SELECT * FROM {NODES};")).all_records, key=repr)


async def _via_transaction(conn: Conn, method: str, ref: str, data: dict[str, Any]) -> None:
    async with conn.transaction() as tx:
        if method == "delete":
            await tx.delete(ref)
        else:
            await getattr(tx, method)(ref, data)


async def _via_rpc(conn: Conn, method: str, ref: str, data: dict[str, Any]) -> None:
    if method == "delete":
        await conn.delete(ref)
    else:
        await getattr(conn, method)(ref, data)


PATHS = {
    "http_transaction": ("http", _via_transaction),
    "ws_transaction": ("ws", _via_transaction),
    "http_rpc": ("http", _via_rpc),
    "ws_rpc": ("ws", _via_rpc),
}


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("ref", REFERENCES)
async def test_reference_names_the_inline_record(http: HTTPConnection, ws: WebSocketConnection, path: str, ref: str) -> None:
    conn_name, drive = PATHS[path]
    conn: Conn = http if conn_name == "http" else ws
    target = RecordId(table=NODES, id=REFERENCES[ref])

    await http.query(f"CREATE {NODES}:keep SET v = 0; CREATE {ref} SET v = 0;")
    keep = {"id": RecordId(table=NODES, id="keep"), "v": 0}

    await drive(conn, "update", ref, {"v": 1})
    assert await _rows(http) == sorted([keep, {"id": target, "v": 1}], key=repr)

    await drive(conn, "merge", ref, {"m": 2})
    assert await _rows(http) == sorted([keep, {"id": target, "v": 1, "m": 2}], key=repr)

    if path.endswith("_rpc"):
        selected = await conn.select(ref)
        assert selected.records == [{"id": target, "v": 1, "m": 2}]

    await drive(conn, "delete", ref, {})
    assert await _rows(http) == [keep]

    await drive(conn, "create", ref, {"v": 3})
    assert (await http.query(f"SELECT * FROM {ref};")).all_records == [{"id": target, "v": 3}]
    assert len(await _rows(http)) == 2


@pytest.mark.parametrize("conn_name", ["http", "ws"])
@pytest.mark.parametrize("generator", ["rand()", "ulid()", "uuid()"])
async def test_rpc_refuses_an_id_generator(
    http: HTTPConnection, ws: WebSocketConnection, conn_name: str, generator: str
) -> None:
    """An RPC call takes a record ID: the server would store the text ``"ulid()"`` as the ID."""
    conn: Conn = http if conn_name == "http" else ws
    with pytest.raises(ValueError, match="ID generator"):
        await conn.create(f"{NODES}:{generator}", {"v": 1})
    assert await _rows(http) == []


async def _profile_rows(conn: HTTPConnection) -> list[dict[str, Any]]:
    return sorted((await conn.query(f"SELECT * FROM `{PROFILES}`;")).all_records, key=repr)


@pytest.mark.parametrize("conn_name", ["http", "ws"])
@pytest.mark.parametrize(
    ("ref", "record_id"),
    [
        (f"{PROFILES}:abc", "abc"),
        (f"{PROFILES}:1", 1),
        (f"`{PROFILES}`:abc", "abc"),
        (f"\u27e8{PROFILES}\u27e9:abc", "abc"),
    ],
)
async def test_rpc_accepts_a_table_that_is_not_an_identifier(
    http: HTTPConnection, ws: WebSocketConnection, conn_name: str, ref: str, record_id: int | str
) -> None:
    """The table is data in the ``RecordId``: ``user-profiles:abc`` reached the server before #324 and still does."""
    conn: Conn = http if conn_name == "http" else ws
    target = RecordId(table=PROFILES, id=record_id)
    inline = f"`{PROFILES}`:{'abc' if record_id == 'abc' else record_id}"
    await http.query(f"CREATE `{PROFILES}`:keep SET v = 0; CREATE {inline} SET v = 0;")
    keep = {"id": RecordId(table=PROFILES, id="keep"), "v": 0}

    await conn.upsert(ref, {"v": 1})
    await conn.merge(ref, {"m": 2})
    assert (await conn.select(ref)).records == [{"id": target, "v": 1, "m": 2}]
    assert await _profile_rows(http) == sorted([keep, {"id": target, "v": 1, "m": 2}], key=repr)

    await conn.delete(ref)
    assert await _profile_rows(http) == [keep]


async def test_http_transaction_still_requires_an_identifier_table(http: HTTPConnection) -> None:
    """``HTTPTransaction`` writes the table into its statement: a name that is not an identifier is refused."""
    with pytest.raises(ValueError, match="the table must be an identifier"):
        async with http.transaction() as tx:
            await tx.update(f"{PROFILES}:abc", {"v": 1})
    assert await _profile_rows(http) == []


# --- the ORM ---------------------------------------------------------------------


@pytest.fixture
async def orm(http: HTTPConnection) -> AsyncGenerator[HTTPConnection, None]:
    """Point the ORM at this module's database (HTTP, CBOR: the defaults)."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE, SURREALDB_DATABASE
    )
    yield http
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


async def _item_rows(conn: HTTPConnection) -> list[dict[str, Any]]:
    return sorted((await conn.query(f"SELECT * FROM {ITEMS};")).all_records, key=repr)


@pytest.mark.parametrize("key", ["123", "-5", "a b", "plain"])
async def test_orm_string_id_stays_a_string_id(orm: HTTPConnection, key: str) -> None:
    """The ORM writes every ID through ``format_thing()``, so digits stay a string ID on the RPC path."""
    target = RecordId(table=ITEMS, id=key)
    item = I324Item(id=key, amount=1, label="a")
    await item.save()
    assert await _item_rows(orm) == [{"id": target, "amount": 1, "label": "a"}]

    loaded = await I324Item.objects().get(key)
    assert (loaded.id, loaded.amount) == (key, 1)

    await loaded.merge(amount=2)
    loaded.label = "b"
    await loaded.save()
    await loaded.refresh()
    assert (loaded.amount, loaded.label) == (2, "b")
    assert await _item_rows(orm) == [{"id": target, "amount": 2, "label": "b"}]

    await loaded.delete()
    assert await _item_rows(orm) == []


async def test_orm_merge_and_save_of_a_saved_record_in_an_http_transaction(orm: HTTPConnection) -> None:
    item = I324Item(id="tx", amount=1, label="a")
    await item.save()
    other = I324Item(id="other", amount=1, label="a")
    await other.save()

    async with await SurrealDBConnectionManager.transaction() as tx:
        await item.merge(tx=tx, amount=5)
        item.label = "b"
        await item.save(tx=tx)

    assert await _item_rows(orm) == [
        {"id": RecordId(table=ITEMS, id="other"), "amount": 1, "label": "a"},
        {"id": RecordId(table=ITEMS, id="tx"), "amount": 5, "label": "b"},
    ]


async def test_orm_model_on_a_table_that_is_not_an_identifier(orm: HTTPConnection) -> None:
    """``format_thing()`` leaves the table as is, so the model API sends ``i324-profiles:<id>`` to the RPC methods."""
    profile = I324Profile(id="p1", amount=1)
    await profile.save()
    target = RecordId(table=PROFILES, id="p1")
    assert await _profile_rows(orm) == [{"id": target, "amount": 1}]

    profile.amount = 2
    await profile.save()
    await profile.merge(amount=3)
    await profile.refresh()
    assert profile.amount == 3
    assert await _profile_rows(orm) == [{"id": target, "amount": 3}]

    await profile.delete()
    assert await _profile_rows(orm) == []
