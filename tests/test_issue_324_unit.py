r"""Unit tests for #324: one reading of a ``table:id`` reference across the SDK.

``HTTPTransaction`` reads a reference strictly (#306): an ID in the signed
64-bit range is an integer, a backtick- or angle-bracket-delimited ID is read
back once, and a malformed reference raises ``ValueError``. The RPC methods of
both connections converted the same string with their own ``_to_thing()``,
which split at the first ``:`` and only stripped backticks, so ``t:1`` named
the string ID ``"1"`` (another record than in a transaction) and ``t:⟨a b⟩``
kept its brackets as part of the ID.

Both paths now go through one private parser,
``surreal_sdk.utils._parse_record_reference()``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.surreal_orm.utils import format_thing
from src.surreal_sdk.connection.http import HTTPConnection
from src.surreal_sdk.connection.websocket import WebSocketConnection
from src.surreal_sdk.protocol.cbor import RecordId
from src.surreal_sdk.utils import _parse_record_reference

T = "i324_nodes"

CONNECTIONS = [HTTPConnection, WebSocketConnection]

#: (reference, ID the server must receive) for the CBOR protocol.
REFERENCES: list[tuple[str, int | str]] = [
    (f"{T}:1", 1),
    (f"{T}:-5", -5),
    (f"{T}:0", 0),
    (f"{T}:9223372036854775807", 2**63 - 1),
    (f"{T}:-9223372036854775808", -(2**63)),
    # Outside the i64 range the server reads the digits as a string ID.
    (f"{T}:9223372036854775808", "9223372036854775808"),
    (f"{T}:-9223372036854775809", "-9223372036854775809"),
    # Not the canonical spelling of an integer: a string ID.
    (f"{T}:05", "05"),
    (f"{T}:+5", "+5"),
    (f"{T}:⟨a b⟩", "a b"),
    (f"{T}:`a b`", "a b"),
    (f"{T}:`1`", "1"),
    (f"{T}:⟨1⟩", "1"),
    (f"{T}:plain_id", "plain_id"),
    (f"{T}:a-b", "a-b"),
    (f"{T}:a:b", "a:b"),
    (format_thing(T, "-5"), "-5"),
    (format_thing(T, "a`b"), "a`b"),
    (format_thing(T, "dir\\"), "dir\\"),
]

MALFORMED = [
    f"{T}:",
    ":x",
    "a b:c",
    "`t`:x",
    f"{T}:`a b",
    f"{T}:`a`b`",
    f"{T}:⟨a b",
    f"{T}:⟨a⟩b⟩",
    f"{T}:``",
    f"{T}:⟨⟩",
    f"{T}:[1, 2]",
    f"{T}:{{a: 1}}",
]

GENERATORS = [f"{T}:rand()", f"{T}:ulid()", f"{T}:uuid()"]


def _conn(cls: type, protocol: str = "cbor") -> Any:
    return cls("http://localhost:8000", "ns", "db", protocol=protocol)


class TestParser:
    @pytest.mark.parametrize(("thing", "record_id"), REFERENCES)
    def test_reads_the_id(self, thing: str, record_id: int | str) -> None:
        ref = _parse_record_reference(thing, "thing")
        assert (ref.table, ref.id) == (T, record_id)
        assert type(ref.id) is type(record_id)

    def test_bare_table(self) -> None:
        ref = _parse_record_reference(T, "thing")
        assert (ref.table, ref.id) == (T, None)

    @pytest.mark.parametrize("thing", MALFORMED)
    def test_malformed_reference_raises(self, thing: str) -> None:
        with pytest.raises(ValueError, match="Invalid thing"):
            _parse_record_reference(thing, "thing")

    @pytest.mark.parametrize("thing", GENERATORS)
    def test_generator_is_flagged(self, thing: str) -> None:
        assert _parse_record_reference(thing, "thing").generator

    def test_delimited_generator_is_a_string_id(self) -> None:
        ref = _parse_record_reference(f"{T}:`ulid()`", "thing")
        assert (ref.id, ref.generator) == ("ulid()", False)


class TestToThing:
    @pytest.mark.parametrize("cls", CONNECTIONS)
    @pytest.mark.parametrize(("thing", "record_id"), REFERENCES)
    def test_cbor_reference_becomes_the_same_record_id(self, cls: type, thing: str, record_id: int | str) -> None:
        result = _conn(cls)._to_thing(thing)
        assert result == RecordId(table=T, id=record_id)
        assert type(result.id) is type(record_id)

    @pytest.mark.parametrize("cls", CONNECTIONS)
    def test_bare_table_stays_a_string(self, cls: type) -> None:
        assert _conn(cls)._to_thing(T) == T

    @pytest.mark.parametrize("cls", CONNECTIONS)
    @pytest.mark.parametrize("thing", [t for t in MALFORMED if ":" in t])
    def test_malformed_reference_raises(self, cls: type, thing: str) -> None:
        with pytest.raises(ValueError, match="Invalid thing"):
            _conn(cls)._to_thing(thing)

    @pytest.mark.parametrize("cls", CONNECTIONS)
    @pytest.mark.parametrize("thing", GENERATORS)
    def test_generator_raises(self, cls: type, thing: str) -> None:
        """The RPC methods take a record ID, not SurrealQL: the server would store ``"ulid()"`` as the ID."""
        with pytest.raises(ValueError, match="ID generator"):
            _conn(cls)._to_thing(thing)

    @pytest.mark.parametrize("cls", CONNECTIONS)
    def test_record_id_passes_through(self, cls: type) -> None:
        rid = RecordId(table=T, id=[1, 2])
        assert _conn(cls)._to_thing(rid) is rid

    @pytest.mark.parametrize("cls", CONNECTIONS)
    @pytest.mark.parametrize("thing", [f"{T}:1", f"{T}:⟨a b⟩", f"{T}:`a", f"{T}:ulid()", T])
    def test_json_protocol_sends_the_string_unchanged(self, cls: type, thing: str) -> None:
        assert _conn(cls, "json")._to_thing(thing) == thing


class TestRpcMethods:
    """Every CRUD method converts its reference before the request, and refuses a malformed one."""

    @pytest.mark.parametrize("cls", CONNECTIONS)
    async def test_methods_send_the_integer_id(self, cls: type) -> None:
        conn = _conn(cls)
        conn.rpc = AsyncMock(return_value=[])
        target = RecordId(table=T, id=1)
        for call in (
            conn.select(f"{T}:1"),
            conn.create(f"{T}:1", {"v": 1}),
            conn.update(f"{T}:1", {"v": 1}),
            conn.upsert(f"{T}:1", {"v": 1}),
            conn.merge(f"{T}:1", {"v": 1}),
            conn.patch(f"{T}:1", []),
            conn.delete(f"{T}:1"),
        ):
            await call
            assert conn.rpc.await_args.args[1][0] == target
            assert type(conn.rpc.await_args.args[1][0].id) is int
        await conn.relate(f"{T}:1", "i324_links", f"{T}:⟨a b⟩")
        params = conn.rpc.await_args.args[1]
        assert params[0] == target
        assert params[2] == RecordId(table=T, id="a b")

    @pytest.mark.parametrize("cls", CONNECTIONS)
    async def test_malformed_reference_raises_before_the_request(self, cls: type) -> None:
        conn = _conn(cls)
        conn.rpc = AsyncMock(return_value=[])
        thing = f"{T}:`a"
        for call in (
            conn.select(thing),
            conn.create(thing),
            conn.update(thing, {}),
            conn.upsert(thing, {}),
            conn.merge(thing, {}),
            conn.patch(thing, []),
            conn.delete(thing),
            conn.relate(thing, "i324_links", f"{T}:b"),
            conn.relate(f"{T}:b", "i324_links", thing),
        ):
            with pytest.raises(ValueError):
                await call
        conn.rpc.assert_not_called()
