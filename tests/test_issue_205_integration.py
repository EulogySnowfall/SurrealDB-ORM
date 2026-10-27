"""Integration tests for #205: ``live()`` filter values are bound, like ``exec()``.

A ``LIVE SELECT`` used to receive its filter values rendered into the SQL text.
Some types had no faithful rendering — a dict became a Python repr, a
``Decimal`` a quoted string — so those filters never matched anything.
SurrealDB 3.x evaluates bound parameters in a ``LIVE SELECT`` WHERE clause, so
every value now travels as a query variable.

Each test opens the stream, writes a record that must *not* match, then one
that must, and checks that the first notification is the matching record.
Notifications on one connection arrive in order, so a filter that wrongly
matched the first write would deliver it first.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, ModelChangeEvent, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.fields.relation import ForeignKey
from src.surreal_orm.query_set import QuerySet
from src.surreal_orm.subquery import Subquery
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.streaming.live_select import LiveSubscriptionParams
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_205"

#: A record id with spaces, quotes, a semicolon and backticks.
AWKWARD_ID = "a b; 'c' \"d\" `e`"

WHEN = datetime(2026, 2, 1, 12, 30, tzinfo=UTC)
OTHER_WHEN = datetime(2025, 1, 1, tzinfo=UTC)
UID = UUID("12345678-1234-5678-1234-567812345678")
OTHER_UID = UUID("87654321-4321-8765-4321-876543218765")


class I205Author(BaseSurrealModel):
    """Target of the foreign key."""

    model_config = SurrealConfigDict(table_name="i205_authors")

    id: str | None = None
    name: str = Field(default="")


class I205Item(BaseSurrealModel):
    """One field per kind of value a live filter can carry."""

    model_config = SurrealConfigDict(table_name="i205_items")

    id: str | None = None
    tag: str | None = None
    prefs: dict[str, Any] | None = None
    price: Decimal | None = None
    author: ForeignKey("I205Author") = None  # type: ignore[valid-type]
    when: datetime | None = None
    uid: UUID | None = None
    day: date | None = None
    tags: list[str] | None = None
    note: str | None = None


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
async def clean_tables() -> AsyncGenerator[None, Any]:
    """Fresh tables (a LIVE SELECT needs the table to exist) and two authors."""
    client = await SurrealDBConnectionManager.get_client()
    await client.query("REMOVE TABLE IF EXISTS i205_items; REMOVE TABLE IF EXISTS i205_authors;")
    await client.query("DEFINE TABLE i205_items SCHEMALESS; DEFINE TABLE i205_authors SCHEMALESS;")
    await client.query("CREATE i205_authors:alice SET name = 'Alice'; CREATE i205_authors:bob SET name = 'Bob';")
    yield
    await client.query("REMOVE TABLE IF EXISTS i205_items; REMOVE TABLE IF EXISTS i205_authors;")


async def _write(sql: str, variables: dict[str, Any] | None = None) -> None:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(sql, variables or {})
    assert response.is_ok, response.raw


async def _first_event(
    qs: QuerySet[I205Item],
    decoy: tuple[str, dict[str, Any]],
    match: tuple[str, dict[str, Any]],
) -> ModelChangeEvent[I205Item] | None:
    """Open ``qs.live()``, write the decoy then the match, return the first event."""
    async with qs.live() as stream:
        await _write(*decoy)
        await _write(*match)
        try:
            return await asyncio.wait_for(anext(stream), timeout=5.0)
        except TimeoutError:
            return None


def _creates(field: str, decoy_value: Any, match_value: Any) -> tuple[tuple[str, dict[str, Any]], ...]:
    """A decoy record and a matching record differing only in *field*."""
    return (
        (f"CREATE i205_items:decoy SET {field} = $v", {"v": decoy_value}),
        (f"CREATE i205_items:match SET {field} = $v", {"v": match_value}),
    )


def _assert_matched(event: ModelChangeEvent[I205Item] | None) -> None:
    assert event is not None, "the live filter never fired for the matching record"
    assert event.record_id == "i205_items:match", f"the first notification was {event.record_id}"


# ==================== Values that used to never match ====================


@pytest.mark.integration
async def test_live_filter_on_dict_value_receives_create() -> None:
    """``filter(prefs={...}).live()`` used to compare against a Python repr string."""
    decoy, match = _creates("prefs", {"theme": "light"}, {"theme": "dark"})
    event = await _first_event(I205Item.objects().filter(prefs={"theme": "dark"}), decoy, match)
    _assert_matched(event)
    assert event is not None and event.instance.prefs == {"theme": "dark"}


@pytest.mark.integration
async def test_live_filter_on_decimal_value_receives_create() -> None:
    """A ``Decimal`` used to be compared as a quoted string."""
    event = await _first_event(
        I205Item.objects().filter(price=Decimal("9.99")),
        ("CREATE i205_items:decoy SET price = 1.50dec", {}),
        ("CREATE i205_items:match SET price = 9.99dec", {}),
    )
    _assert_matched(event)
    assert event is not None and event.instance.price == Decimal("9.99")


@pytest.mark.integration
async def test_decimal_binds_as_a_surrealdb_decimal() -> None:
    """A bound ``Decimal`` reaches the server as a ``decimal``, every digit kept.

    This is what the live filter above depends on, and ``exec()`` with it.
    """
    digits = Decimal("123.4567890123456789012345")
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query("RETURN [type::of($p), $p]", {"p": digits})
    assert response.results[0].result == ["decimal", digits]

    await _write("CREATE i205_items:match SET price = 9.99dec; CREATE i205_items:decoy SET price = 1.50dec")
    found = await I205Item.objects().filter(price=Decimal("9.99")).exec()
    assert [item.id for item in found] == ["match"]


@pytest.mark.integration
async def test_date_binds_as_a_surrealdb_datetime() -> None:
    """A bound ``date`` reaches the server as the datetime ``d'YYYY-MM-DD'`` denotes.

    The live filter on a ``date`` depends on this, and so do ``save()`` and
    ``exec()`` with one: the request used to be rejected outright.
    """
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query("RETURN [type::of($d), $d = d'2026-02-01']", {"d": date(2026, 2, 1)})
    assert response.results[0].result == ["datetime", True]

    await I205Item(id="match", day=date(2026, 2, 1)).save()
    await I205Item(id="decoy", day=date(2025, 1, 1)).save()
    found = await I205Item.objects().filter(day=date(2026, 2, 1)).exec()
    assert [(item.id, item.day) for item in found] == [("match", date(2026, 2, 1))]


# ==================== Every other value type still matches ====================


def _author(record_id: str) -> RecordId:
    return RecordId(table="i205_authors", id=record_id)


#: (test id, filter kwargs, (field, decoy value, matching value))
_STILL_MATCHING: list[tuple[str, dict[str, Any], tuple[str, Any, Any]]] = [
    ("fk_bare_id", {"author": "alice"}, ("author", _author("bob"), _author("alice"))),
    ("fk_table_id", {"author": "i205_authors:alice"}, ("author", _author("bob"), _author("alice"))),
    ("fk_record_id", {"author": _author("alice")}, ("author", _author("bob"), _author("alice"))),
    ("fk_digit_id", {"author": "7qvdzsc14e5clo8sg064"}, ("author", _author("bob"), _author("7qvdzsc14e5clo8sg064"))),
    ("fk_in", {"author__in": ["alice", "i205_authors:carol"]}, ("author", _author("bob"), _author("alice"))),
    ("datetime", {"when": WHEN}, ("when", OTHER_WHEN, WHEN)),
    ("uuid", {"uid": UID}, ("uid", OTHER_UID, UID)),
    ("date", {"day": date(2026, 2, 1)}, ("day", datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))),
    ("list_in", {"tag__in": ["x", "y"]}, ("tag", "z", "y")),
    ("list_exact", {"tags": ["a", "b"]}, ("tags", ["b", "a"], ["a", "b"])),
    ("string_with_quotes", {"tag": 'it\'s "quoted" \\ here'}, ("tag", "it's", 'it\'s "quoted" \\ here')),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("filters", "writes"),
    [(f, w) for _, f, w in _STILL_MATCHING],
    ids=[name for name, _, _ in _STILL_MATCHING],
)
async def test_live_filter_value_types_still_match(filters: dict[str, Any], writes: tuple[str, Any, Any]) -> None:
    decoy, match = _creates(*writes)
    event = await _first_event(I205Item.objects().filter(**filters), decoy, match)
    _assert_matched(event)


@pytest.mark.integration
async def test_live_filter_on_model_instance_matches() -> None:
    """The related instance itself is accepted, Django-style."""
    alice = await I205Author.objects().get("alice")
    decoy, match = _creates("author", _author("bob"), _author("alice"))
    event = await _first_event(I205Item.objects().filter(author=alice), decoy, match)
    _assert_matched(event)


@pytest.mark.integration
async def test_live_filter_on_none_matches() -> None:
    """``filter(note=None)`` matches a record that has no ``note``."""
    event = await _first_event(
        I205Item.objects().filter(note=None),
        ("CREATE i205_items:decoy SET note = 'set'", {}),
        ("CREATE i205_items:match SET tag = 'x'", {}),
    )
    _assert_matched(event)


@pytest.mark.integration
async def test_live_filter_values_are_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """An id with spaces, ``;``, quotes and backticks reaches the server as a variable.

    The emitted SQL holds only ``$name`` references; the id travels in the
    bindings, untouched, and selects that exact record — not one whose id is a
    prefix of it.
    """
    ws = await SurrealDBConnectionManager.get_ws_client()
    sent: list[tuple[str, dict[str, Any] | None]] = []
    original_query = ws.query

    async def recording_query(sql: str, vars: dict[str, Any] | None = None) -> Any:
        sent.append((sql, vars))
        return await original_query(sql, vars)

    monkeypatch.setattr(ws, "query", recording_query)

    event = await _first_event(
        I205Item.objects().filter(author=AWKWARD_ID),
        ("CREATE i205_items:decoy SET author = $v", {"v": _author("a b")}),
        ("CREATE i205_items:match SET author = $v", {"v": _author(AWKWARD_ID)}),
    )

    live_queries = [(sql, v) for sql, v in sent if sql.startswith("LIVE SELECT")]
    assert live_queries == [
        ("LIVE SELECT * FROM i205_items WHERE author = $_f0", {"_f0": _author(AWKWARD_ID)}),
    ]
    _assert_matched(event)
    assert event is not None and event.instance.author == f"i205_authors:{AWKWARD_ID}"


# ==================== Filters that reference other variables ====================


@pytest.mark.integration
async def test_live_filter_with_variables_reference() -> None:
    """A ``$name`` from ``.variables()`` is bound next to the filter values."""
    decoy, match = _creates("tag", "other", "wanted")
    event = await _first_event(I205Item.objects().filter(tag="$want").variables(want="wanted"), decoy, match)
    _assert_matched(event)


@pytest.mark.integration
async def test_live_filter_with_subquery() -> None:
    """A ``Subquery`` stays inline in the WHERE; its own values are bound too."""
    inner = I205Author.objects().filter(name="Alice").select("id")
    decoy, match = _creates("author", _author("bob"), _author("alice"))
    event = await _first_event(I205Item.objects().filter(author__in=Subquery(inner)), decoy, match)
    _assert_matched(event)


# ==================== Auto-resubscribe ====================


@pytest.mark.integration
async def test_resubscribe_after_reconnect_keeps_bound_filter() -> None:
    """After the socket drops, the re-created LIVE SELECT applies the same bound filter."""
    conn = WebSocketConnection(SURREALDB_URL, SURREALDB_NAMESPACE, SURREALDB_DATABASE, reconnect_interval=0.05)
    await conn.connect()
    try:
        await conn.signin(SURREALDB_USER, SURREALDB_PASS)

        received: list[dict[str, Any]] = []
        got_one = asyncio.Event()
        reconnected: list[tuple[str, str]] = []
        resubscribed = asyncio.Event()

        async def on_change(notification: dict[str, Any]) -> None:
            received.append(notification)
            got_one.set()

        async def on_reconnect(old_id: str, new_id: str) -> None:
            reconnected.append((old_id, new_id))
            resubscribed.set()

        subscription = LiveSubscriptionParams(
            table="i205_items",
            where="prefs = $_f0 AND price = $_f1",
            params={"_f0": {"theme": "dark"}, "_f1": Decimal("9.99")},
            diff=False,
            callback=on_change,
            on_reconnect=on_reconnect,
        )
        first_id = await conn._resubscribe_one(subscription)

        assert conn._ws is not None
        await conn._ws.close()  # the server side of the socket is gone; the reader reconnects
        await asyncio.wait_for(resubscribed.wait(), timeout=15.0)
        assert reconnected[0][0] == first_id and reconnected[0][1] != first_id

        await _write("CREATE i205_items:decoy SET prefs = {theme: 'dark'}, price = 1.50dec")
        await _write("CREATE i205_items:match SET prefs = {theme: 'dark'}, price = 9.99dec")
        await asyncio.wait_for(got_one.wait(), timeout=5.0)

        assert str(received[0]["result"]["id"]) == "i205_items:match"
    finally:
        await conn.close()
