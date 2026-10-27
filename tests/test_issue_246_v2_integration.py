"""Integration tests for #246: ``live()`` filter values render like bound parameters.

SurrealDB 2.x does not evaluate bound parameters in a ``LIVE SELECT`` WHERE
clause, so on this line every filter value is written into the query text by
``LiveSelectStream._format_value``. Each literal must designate exactly what the
same value bound as a parameter designates. Some types had no faithful form —
a dict became a Python repr, a ``Decimal`` a quoted string — so those filters
never matched anything.

Two kinds of checks:

* ``RETURN <literal> = $r`` with the value bound as ``$r``: the rendered text
  and the bound value are the same SurrealDB value.
* A live stream opened on a filter, then a record that must *not* match
  written before one that must; the first notification is the matching record.
  Notifications on one connection arrive in order, so a filter that wrongly
  matched the first write would deliver it first.

The same file covers the CBOR side: a bound ``Decimal`` or ``date`` used to be
rejected by the server for the whole request.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta, timezone
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
from surreal_sdk.streaming.live_select import LiveSelectStream, LiveSubscriptionParams
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_issue_246_v2"

WHEN = datetime(2026, 2, 1, 12, 30, tzinfo=UTC)
OTHER_WHEN = datetime(2025, 1, 1, tzinfo=UTC)
UID = UUID("12345678-1234-5678-1234-567812345678")
OTHER_UID = UUID("87654321-4321-8765-4321-876543218765")
#: The largest magnitude of a SurrealDB decimal.
DECIMAL_MAX = Decimal("79228162514264337593543950335")
#: Written out: ``-DECIMAL_MAX`` would round to the 28-digit default context.
DECIMAL_MIN = Decimal("-79228162514264337593543950335")

#: Characters a record id or table name may hold that a bare identifier cannot.
SPECIAL_NAMES: list[tuple[str, str]] = [
    ("space", "a b"),
    ("semicolon", "a;b"),
    ("quote", "a'b"),
    ("double-quote", 'a"b'),
    ("backtick", "a`b"),
    ("backslash", "a\\b"),
    ("trailing-backslash", "a\\"),
    ("dollar", "a$b"),
    ("dash", "a-b"),
    ("mixed", 'x` , 1, `y \\\' "z" ;'),
]


class I246Author(BaseSurrealModel):
    """Target of the foreign key."""

    model_config = SurrealConfigDict(table_name="i246_authors")

    id: str | None = None
    name: str = Field(default="")


class I246Item(BaseSurrealModel):
    """One field per kind of value a live filter can carry."""

    model_config = SurrealConfigDict(table_name="i246_items")

    id: str | None = None
    tag: str | None = None
    prefs: dict[str, Any] | None = None
    price: Decimal | None = None
    author: ForeignKey("I246Author") = None  # type: ignore[valid-type]
    ref: str | None = None
    when: datetime | None = None
    uid: UUID | None = None
    #: ``Any``, not ``date``: reading a datetime back into a ``date`` field is #231 on this line.
    day: Any = None
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
    await client.query("REMOVE TABLE IF EXISTS i246_items; REMOVE TABLE IF EXISTS i246_authors;")
    await client.query("DEFINE TABLE i246_items SCHEMALESS; DEFINE TABLE i246_authors SCHEMALESS;")
    await client.query("CREATE i246_authors:alice SET name = 'Alice'; CREATE i246_authors:bob SET name = 'Bob';")
    yield
    await client.query("REMOVE TABLE IF EXISTS i246_items; REMOVE TABLE IF EXISTS i246_authors;")


async def _write(sql: str, variables: dict[str, Any] | None = None) -> None:
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(sql, variables or {})
    assert response.is_ok, response.raw


async def _first_event(
    qs: QuerySet[I246Item],
    decoy: tuple[str, dict[str, Any]],
    match: tuple[str, dict[str, Any]],
) -> ModelChangeEvent[I246Item] | None:
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
        (f"CREATE i246_items:decoy SET {field} = $v", {"v": decoy_value}),
        (f"CREATE i246_items:match SET {field} = $v", {"v": match_value}),
    )


def _assert_matched(event: ModelChangeEvent[I246Item] | None) -> None:
    assert event is not None, "the live filter never fired for the matching record"
    assert event.record_id == "i246_items:match", f"the first notification was {event.record_id}"


def _author(record_id: Any) -> RecordId:
    return RecordId(table="i246_authors", id=record_id)


# ==================== Each literal is the bound value ====================

#: (test id, value rendered into the query, value bound as $r)
_LITERALS: list[tuple[str, Any, Any]] = [
    ("none", None, None),
    ("true", True, True),
    ("false", False, False),
    ("int", 42, 42),
    ("int-negative", -5, -5),
    ("int-max", 2**63 - 1, 2**63 - 1),
    ("int-min", -(2**63), -(2**63)),
    ("float", 3.14, 3.14),
    ("float-whole", 3.0, 3.0),
    ("float-exponent", 1e20, 1e20),
    ("float-small", -1e-05, -1e-05),
    ("decimal", Decimal("9.99"), Decimal("9.99")),
    ("decimal-negative", Decimal("-1.50"), Decimal("-1.50")),
    ("decimal-long", Decimal("123.4567890123456789012345"), Decimal("123.4567890123456789012345")),
    ("decimal-exponent", Decimal("1E+2"), Decimal("1E+2")),
    ("decimal-small", Decimal("1E-5"), Decimal("1E-5")),
    ("decimal-max", DECIMAL_MAX, DECIMAL_MAX),
    ("decimal-min", DECIMAL_MIN, DECIMAL_MIN),
    ("decimal-smallest-step", Decimal("1E-28"), Decimal("1E-28")),
    ("decimal-28-places", Decimal("0.1234567890123456789012345678"), Decimal("0.1234567890123456789012345678")),
    ("decimal-rounded", Decimal("0.00000000000000000000000000025"), Decimal("0.00000000000000000000000000025")),
    ("string", "plain", "plain"),
    ("string-quote", "it's", "it's"),
    ("string-double-quote", 'say "hi"', 'say "hi"'),
    ("string-backslash", "C:\\dir\\file", "C:\\dir\\file"),
    ("string-trailing-backslash", "ends\\", "ends\\"),
    ("string-backslash-quote", "\\'", "\\'"),
    ("string-backtick", "a `b`", "a `b`"),
    ("string-dollar", "costs $other", "costs $other"),
    ("string-group-ref", "\\g<0> \\1", "\\g<0> \\1"),
    ("string-newline", "line\nbreak\ttab", "line\nbreak\ttab"),
    ("string-unicode", "café 😀", "café 😀"),
    ("uuid", UID, UID),
    ("datetime", WHEN, WHEN),
    ("datetime-offset", datetime(2026, 2, 1, 14, 30, tzinfo=timezone(timedelta(hours=2))), WHEN),
    (
        "datetime-micro",
        datetime(2026, 2, 1, 12, 30, 0, 123456, tzinfo=UTC),
        datetime(2026, 2, 1, 12, 30, 0, 123456, tzinfo=UTC),
    ),
    ("datetime-naive", datetime(2026, 2, 1, 12, 30), WHEN),
    ("date", date(2026, 2, 1), date(2026, 2, 1)),
    ("list", [1, "a", None, [True]], [1, "a", None, [True]]),
    ("tuple", ("x", 2), ["x", 2]),
    ("list-empty", [], []),
    ("dict", {"theme": "dark"}, {"theme": "dark"}),
    (
        "dict-keys",
        {"a b": 1, "c'd": 2, "e`f": 3, "": 4, "g\\": 5, "$h": 6},
        {"a b": 1, "c'd": 2, "e`f": 3, "": 4, "g\\": 5, "$h": 6},
    ),
    ("dict-nested", {"n": {"price": Decimal("1.5"), "at": [WHEN]}}, {"n": {"price": Decimal("1.5"), "at": [WHEN]}}),
    ("dict-empty", {}, {}),
    ("record", RecordId("t", "abc"), RecordId("t", "abc")),
    ("record-int", RecordId("t", 123), RecordId("t", 123)),
    ("record-int-zero", RecordId("t", 0), RecordId("t", 0)),
    ("record-int-negative", RecordId("t", -5), RecordId("t", -5)),
    ("record-digit-string", RecordId("t", "123"), RecordId("t", "123")),
    ("record-digit-prefix", RecordId("t", "7qvdzsc14e5clo8sg064"), RecordId("t", "7qvdzsc14e5clo8sg064")),
    ("record-empty", RecordId("t", ""), RecordId("t", "")),
    ("record-array", RecordId("t", ["a", 1]), RecordId("t", ["a", 1])),
    ("record-object", RecordId("t", {"k": "v", "n": 1}), RecordId("t", {"k": "v", "n": 1})),
    ("record-uuid", RecordId("t", UID), RecordId("t", UID)),
    ("table-digit-prefix", RecordId("123t", "abc"), RecordId("123t", "abc")),
    ("table-keyword", RecordId("select", "abc"), RecordId("select", "abc")),
    *[(f"record-id-{name}", RecordId("t", text), RecordId("t", text)) for name, text in SPECIAL_NAMES],
    *[(f"table-{name}", RecordId(text, "abc"), RecordId(text, "abc")) for name, text in SPECIAL_NAMES],
]


@pytest.mark.integration
@pytest.mark.parametrize(("rendered", "bound"), [(r, b) for _, r, b in _LITERALS], ids=[n for n, _, _ in _LITERALS])
async def test_rendered_literal_equals_the_bound_value(rendered: Any, bound: Any) -> None:
    """``RETURN <literal> = $r`` holds, and the literal reads back as the bound value."""
    literal = LiveSelectStream._format_value(rendered)
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(f"RETURN [{literal} = $r, {literal}, $r]", {"r": bound})
    assert response.is_ok, response.raw
    equal, read_literal, read_bound = response.results[0].result
    assert equal is True, f"{literal} != {bound!r}"
    assert read_literal == read_bound


@pytest.mark.integration
@pytest.mark.parametrize("text", ["users:abc", "2026-01-01T00:00:00Z", "12345678-1234-5678-1234-567812345678"])
async def test_string_that_looks_like_another_type_stays_a_string(text: str) -> None:
    literal = LiveSelectStream._format_value(text)
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(f"RETURN [type::is::string({literal}), {literal} = $r]", {"r": text})
    assert response.results[0].result == [True, True]


@pytest.mark.integration
async def test_trailing_backslash_does_not_reach_the_next_literal() -> None:
    """The closing backtick of an id ending in ``\\`` closes it; what follows stays separate."""
    literal = LiveSelectStream._format_value([RecordId("t", "a\\"), 1, "b`"])
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(f"RETURN {literal}")
    assert response.results[0].result == [RecordId("t", "a\\"), 1, "b`"]


# ==================== Record links in live() ====================

#: (test id, filter value, decoy link, matching link)
_RECORD_FILTERS: list[tuple[str, Any, RecordId, RecordId]] = [
    ("int-id", RecordId("i246_authors", 123), _author("123"), _author(123)),
    ("digit-string-id", "123", _author(123), _author("123")),
    ("digit-prefix-id", "7qvdzsc14e5clo8sg064", _author("7qvdzsc14e5clo8sg064x"), _author("7qvdzsc14e5clo8sg064")),
    *[(f"id-{name}", text, _author("a"), _author(text)) for name, text in SPECIAL_NAMES],
    *[(f"table-{name}", RecordId(text, "abc"), RecordId("a", "abc"), RecordId(text, "abc")) for name, text in SPECIAL_NAMES],
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("value", "decoy", "match"),
    [(v, d, m) for _, v, d, m in _RECORD_FILTERS],
    ids=[n for n, _, _, _ in _RECORD_FILTERS],
)
async def test_live_filter_values_render_exact_literals(value: Any, decoy: RecordId, match: RecordId) -> None:
    """A record link in a live filter matches that record and no other.

    String ids go through the ``ForeignKey`` coercion (a bare id is qualified
    with the target table); the others are passed as ``RecordId``.
    """
    field = "ref" if isinstance(value, RecordId) else "author"
    decoy_write, match_write = _creates(field, decoy, match)
    event = await _first_event(I246Item.objects().filter(**{field: value}), decoy_write, match_write)
    _assert_matched(event)


# ==================== Values that used to never match ====================


@pytest.mark.integration
async def test_live_filter_on_dict_value_receives_create() -> None:
    """``filter(prefs={...}).live()`` used to compare against a Python repr string."""
    decoy, match = _creates("prefs", {"theme": "light"}, {"theme": "dark"})
    event = await _first_event(I246Item.objects().filter(prefs={"theme": "dark"}), decoy, match)
    _assert_matched(event)
    assert event is not None and event.instance.prefs == {"theme": "dark"}


@pytest.mark.integration
async def test_live_filter_on_decimal_value_receives_create() -> None:
    """A ``Decimal`` used to be compared as a quoted string."""
    event = await _first_event(
        I246Item.objects().filter(price=Decimal("9.99")),
        ("CREATE i246_items:decoy SET price = 1.50dec", {}),
        ("CREATE i246_items:match SET price = 9.99dec", {}),
    )
    _assert_matched(event)
    assert event is not None and event.instance.price == Decimal("9.99")


@pytest.mark.integration
async def test_decimal_binds_as_a_surrealdb_decimal() -> None:
    """A bound ``Decimal`` reaches the server as a ``decimal``, every digit kept.

    The request used to be rejected outright (tag 4 is not read by 2.7.0).
    """
    digits = Decimal("123.4567890123456789012345")
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(
        "RETURN [type::is::decimal($p), $p, $q = 1.5dec, $e = 100dec]",
        {"p": digits, "q": Decimal("1.5"), "e": Decimal("1E+2")},
    )
    assert response.results[0].result == [True, digits, True, True]

    await _write("CREATE i246_items:match SET price = 9.99dec; CREATE i246_items:decoy SET price = 1.50dec")
    found = await I246Item.objects().filter(price=Decimal("9.99")).exec()
    assert [item.id for item in found] == ["match"]


@pytest.mark.integration
async def test_decimal_binds_at_the_range_bounds() -> None:
    """The largest magnitude and 28 places reach the server unchanged."""
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query(
        "RETURN [$max = 79228162514264337593543950335dec, $min = -79228162514264337593543950335dec, "
        "$step = 0.0000000000000000000000000001dec, $places = 0.1234567890123456789012345678dec, $max, $places]",
        {
            "max": DECIMAL_MAX,
            "min": DECIMAL_MIN,
            "step": Decimal("1E-28"),
            "places": Decimal("0.1234567890123456789012345678"),
        },
    )
    assert response.is_ok, response.raw
    assert response.results[0].result == [True, True, True, True, DECIMAL_MAX, Decimal("0.1234567890123456789012345678")]


@pytest.mark.integration
@pytest.mark.parametrize(
    "digits",
    [
        "0.00000000000000000000000000025",
        "-0.00000000000000000000000000025",
        "0.00000000000000000000000000035",
        "0.00000000000000000000000000024",
        "0.033333333333333333333333333333",
        "1e-40",
    ],
)
async def test_decimal_rounded_to_28_places_is_what_surrealdb_stores(digits: str) -> None:
    """The client rounds extra places exactly as the server does with the full digits.

    ``$full`` carries every digit in the decimal tag (positional, as 2.7.0
    requires); ``$ours`` is the ``Decimal`` the encoder rounds to 28 places.
    """
    from cbor2 import CBORTag

    full = format(Decimal(digits), "f")
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query("RETURN [$full = $ours, $ours]", {"full": CBORTag(10, full), "ours": Decimal(digits)})
    assert response.is_ok, response.raw
    equal, stored = response.results[0].result
    assert equal is True, f"{digits}: server stored something else than {stored}"


@pytest.mark.integration
@pytest.mark.parametrize("digits", ["1e1000000000", "79228162514264337593543950336", "1E+29"])
async def test_out_of_range_decimal_raises_before_the_request(digits: str) -> None:
    """Out-of-range Decimal values raise instead of being expanded or sent."""
    client = await SurrealDBConnectionManager.get_client()
    with pytest.raises(ValueError, match="out of range"):
        await client.query("RETURN $p", {"p": Decimal(digits)})
    with pytest.raises(ValueError, match="out of range"):
        await I246Item.objects().filter(price=Decimal(digits)).exec()


@pytest.mark.integration
async def test_date_binds_as_a_surrealdb_datetime() -> None:
    """A bound ``date`` reaches the server as the datetime ``d'YYYY-MM-DD'`` denotes.

    ``save()`` and ``exec()`` with a ``date`` used to be rejected outright
    (tag 1004 is not read by 2.7.0).
    """
    client = await SurrealDBConnectionManager.get_client()
    response = await client.query("RETURN [type::is::datetime($d), $d = d'2026-02-01']", {"d": date(2026, 2, 1)})
    assert response.results[0].result == [True, True]

    await I246Item(id="match", day=date(2026, 2, 1)).save()
    await I246Item(id="decoy", day=date(2025, 1, 1)).save()
    found = await I246Item.objects().filter(day=date(2026, 2, 1)).exec()
    assert [item.id for item in found] == ["match"]
    response = await client.query("RETURN (SELECT VALUE day = d'2026-02-01' FROM ONLY i246_items:match)")
    assert response.results[0].result is True


# ==================== Every other value type still matches ====================

#: (test id, filter kwargs, (field, decoy value, matching value))
_STILL_MATCHING: list[tuple[str, dict[str, Any], tuple[str, Any, Any]]] = [
    ("fk_bare_id", {"author": "alice"}, ("author", _author("bob"), _author("alice"))),
    ("fk_table_id", {"author": "i246_authors:alice"}, ("author", _author("bob"), _author("alice"))),
    ("fk_record_id", {"author": _author("alice")}, ("author", _author("bob"), _author("alice"))),
    ("fk_in", {"author__in": ["alice", "i246_authors:carol"]}, ("author", _author("bob"), _author("alice"))),
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
    event = await _first_event(I246Item.objects().filter(**filters), decoy, match)
    _assert_matched(event)


@pytest.mark.integration
async def test_live_filter_string_holding_another_parameter_name() -> None:
    """A ``$name`` inside a string value is text, even when a parameter has that name."""
    event = await _first_event(
        I246Item.objects().filter(tag="costs $_f1", note="x"),
        ("CREATE i246_items:decoy SET tag = $t, note = 'x'", {"t": "costs 'x'"}),
        ("CREATE i246_items:match SET tag = $t, note = 'x'", {"t": "costs $_f1"}),
    )
    _assert_matched(event)


@pytest.mark.integration
async def test_live_filter_on_model_instance_matches() -> None:
    """The related instance itself is accepted, Django-style."""
    alice = await I246Author.objects().get("alice")
    decoy, match = _creates("author", _author("bob"), _author("alice"))
    event = await _first_event(I246Item.objects().filter(author=alice), decoy, match)
    _assert_matched(event)


@pytest.mark.integration
async def test_live_filter_on_none_matches() -> None:
    """``filter(note=None)`` matches a record that has no ``note``."""
    event = await _first_event(
        I246Item.objects().filter(note=None),
        ("CREATE i246_items:decoy SET note = 'set'", {}),
        ("CREATE i246_items:match SET tag = 'x'", {}),
    )
    _assert_matched(event)


# ==================== Filters that reference other variables ====================


@pytest.mark.integration
async def test_live_filter_with_variables_reference() -> None:
    """A ``$name`` from ``.variables()`` is rendered next to the filter values."""
    decoy, match = _creates("tag", "other", "wanted")
    event = await _first_event(I246Item.objects().filter(tag="$want").variables(want="wanted"), decoy, match)
    _assert_matched(event)


@pytest.mark.integration
async def test_live_filter_with_subquery() -> None:
    """A ``Subquery`` stays inline in the WHERE; its own values are rendered too."""
    inner = I246Author.objects().filter(name="Alice").select("id")
    decoy, match = _creates("author", _author("bob"), _author("alice"))
    event = await _first_event(I246Item.objects().filter(author__in=Subquery(inner)), decoy, match)
    _assert_matched(event)


# ==================== Auto-resubscribe ====================


@pytest.mark.integration
async def test_resubscribe_after_reconnect_keeps_the_filter() -> None:
    """After the socket drops, the re-created LIVE SELECT applies the same filter."""
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
            table="i246_items",
            where="prefs = $_f0 AND price = $_f1 AND ref = $_f2",
            params={"_f0": {"theme": "dark"}, "_f1": Decimal("9.99"), "_f2": RecordId("a", "x`y\\")},
            diff=False,
            callback=on_change,
            on_reconnect=on_reconnect,
        )
        first_id = await conn._resubscribe_one(subscription)

        assert conn._ws is not None
        await conn._ws.close()  # the server side of the socket is gone; the reader reconnects
        await asyncio.wait_for(resubscribed.wait(), timeout=15.0)
        assert reconnected[0][0] == first_id and reconnected[0][1] != first_id

        link = {"r": RecordId("a", "x`y\\")}
        await _write("CREATE i246_items:decoy SET prefs = {theme: 'dark'}, price = 1.50dec, ref = $r", link)
        await _write("CREATE i246_items:match SET prefs = {theme: 'dark'}, price = 9.99dec, ref = $r", link)
        await asyncio.wait_for(got_one.wait(), timeout=5.0)

        assert str(received[0]["result"]["id"]) == "i246_items:match"
    finally:
        await conn.close()
