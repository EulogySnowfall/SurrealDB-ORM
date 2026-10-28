r"""Unit tests for #310 (v2 line): record IDs containing a backtick or a backslash.

Inside a backtick-delimited record ID SurrealDB reads ``\`` as an escape
character: ``\``` is a backtick and ``\\`` a backslash. ``escape_record_id()``
only doubled the backticks, which is not an escape sequence the server knows,
and left backslashes as they were. An ID with a backtick therefore failed to
parse, and an ID with a backslash was read differently from what was written.

The helper now escapes the backslash first, then the backtick. The reverse
direction — ``parse_record_id()`` in the ORM, ``_to_thing()`` in both SDK
connections — reads the same two sequences back through one shared helper,
``surreal_sdk.protocol.cbor.unescape_record_id()``.
"""

from __future__ import annotations

import pytest

from src.surreal_orm.fields.relation import RelationInfo
from src.surreal_orm.relations import RelationQuerySet
from src.surreal_orm.utils import escape_record_id, format_thing, parse_record_id
from surreal_sdk.connection.http import HTTPConnection
from surreal_sdk.connection.websocket import WebSocketConnection
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.transaction import _validate_thing
from surreal_sdk.protocol.cbor import unescape_record_id

#: (raw ID, expected literal) — the literal is what SurrealDB must receive.
ESCAPED: list[tuple[str, str]] = [
    ("a`b", r"`a\`b`"),
    ("a\\b", r"`a\\b`"),
    ("trail\\", r"`trail\\`"),
    ("a\\`b", r"`a\\\`b`"),
    ("`", r"`\``"),
    ("\\", r"`\\`"),
    ("``", r"`\`\``"),
    ("with space", "`with space`"),
    ("semi;colon", "`semi;colon`"),
    ("it's", "`it's`"),
    ('say "hi"', '`say "hi"`'),
    ("dash--dash", "`dash--dash`"),
    ("x\\`; --y\\", r"`x\\\`; --y\\`"),
    ("7abc", "`7abc`"),
]

#: Every raw ID of the table above, plus IDs that need no escaping.
RAW_IDS = [raw for raw, _ in ESCAPED] + ["plain", "abc_123", "_private"]


class TestEscapeRecordId:
    @pytest.mark.parametrize(("raw", "expected"), ESCAPED)
    def test_escaped_form(self, raw: str, expected: str) -> None:
        assert escape_record_id(raw) == expected

    @pytest.mark.parametrize("raw", ["plain", "abc_123", "_private", "Users"])
    def test_plain_ids_are_unchanged(self, raw: str) -> None:
        assert escape_record_id(raw) == raw

    @pytest.mark.parametrize("raw", ["7abc", "123", "0test", "test-id", "test.id"])
    def test_ids_without_backtick_or_backslash_keep_their_old_form(self, raw: str) -> None:
        """Only IDs carrying ``\\`` or a backtick produce different SurrealQL."""
        assert escape_record_id(raw) == f"`{raw}`"

    @pytest.mark.parametrize("raw", RAW_IDS)
    def test_literal_never_ends_on_an_escaped_delimiter(self, raw: str) -> None:
        """The closing backtick is preceded by an even run of backslashes."""
        literal = escape_record_id(raw)
        if not literal.startswith("`"):
            return
        body = literal[1:-1]
        trailing = len(body) - len(body.rstrip("\\"))
        assert trailing % 2 == 0
        # Every backtick inside the body is escaped.
        for i, ch in enumerate(body):
            if ch == "`":
                run = len(body[:i]) - len(body[:i].rstrip("\\"))
                assert run % 2 == 1, literal


class TestFormatThing:
    @pytest.mark.parametrize(("raw", "expected"), ESCAPED)
    def test_delegates_to_escape_record_id(self, raw: str, expected: str) -> None:
        assert format_thing("i310_nodes", raw) == f"i310_nodes:{expected}"

    def test_plain_and_digit_leading_ids_unchanged(self) -> None:
        assert format_thing("users", "abc123") == "users:abc123"
        assert format_thing("users", "7abc") == "users:`7abc`"


class TestRecordIdToSurql:
    @pytest.mark.parametrize(("raw", "expected"), ESCAPED)
    def test_uses_the_same_escaping(self, raw: str, expected: str) -> None:
        assert RecordId(table="i310_nodes", id=raw).to_surql() == f"i310_nodes:{expected}"

    @pytest.mark.parametrize("raw", RAW_IDS)
    def test_matches_format_thing(self, raw: str) -> None:
        assert RecordId(table="i310_nodes", id=raw).to_surql() == format_thing("i310_nodes", raw)


class TestReadBack:
    @pytest.mark.parametrize("raw", RAW_IDS)
    def test_parse_record_id_inverts_format_thing(self, raw: str) -> None:
        assert parse_record_id(format_thing("i310_nodes", raw)) == ("i310_nodes", raw)

    @pytest.mark.parametrize("raw", RAW_IDS)
    def test_unescape_inverts_escape(self, raw: str) -> None:
        assert unescape_record_id(escape_record_id(raw)) == raw

    def test_unescape_leaves_a_bare_id_alone(self) -> None:
        assert unescape_record_id("a\\b") == "a\\b"
        assert unescape_record_id("plain") == "plain"

    def test_unescape_still_reads_the_doubled_backtick_form(self) -> None:
        assert unescape_record_id("`test``id`") == "test`id"

    def test_unescape_keeps_other_backslash_sequences(self) -> None:
        """Only ``\\\\`` and ``\\``` are produced, so only they are read back."""
        assert unescape_record_id(r"`a\nb`") == r"a\nb"

    @pytest.mark.parametrize("conn_cls", [HTTPConnection, WebSocketConnection])
    @pytest.mark.parametrize("raw", RAW_IDS)
    def test_sdk_to_thing_recovers_the_raw_id(self, conn_cls: type, raw: str) -> None:
        conn = conn_cls("http://localhost:8000", "ns", "db")
        assert conn._to_thing(format_thing("i310_nodes", raw)) == RecordId(table="i310_nodes", id=raw)


class TestCallersUseTheHelper:
    @pytest.mark.parametrize("raw", ["a`b", "trail\\", "x\\`; --y\\"])
    def test_relation_info_traversal(self, raw: str) -> None:
        info = RelationInfo(to_model="i310_nodes", relation_type="relation", edge_table="i310_links")
        query = info.get_traversal_query("i310_nodes", raw)
        assert query == f"SELECT * FROM {format_thing('i310_nodes', raw)}->i310_links->i310_nodes"

    @pytest.mark.parametrize("raw", ["a`b", "trail\\", "x\\`; --y\\", "7abc", "plain"])
    def test_relation_queryset_start_node(self, raw: str) -> None:
        class _Instance:
            def get_table_name(self) -> str:
                return "i310_nodes"

            def get_id(self) -> str:
                return raw

        info = RelationInfo(to_model="i310_nodes", relation_type="relation", edge_table="i310_links")
        query, _ = RelationQuerySet(_Instance(), info, [(info, {})])._build_traversal_query()  # type: ignore[arg-type]
        assert query == f"SELECT * FROM {format_thing('i310_nodes', raw)}->i310_links->i310_nodes;"


class TestValidateThing:
    @pytest.mark.parametrize("raw", RAW_IDS)
    def test_accepts_the_escaped_form(self, raw: str) -> None:
        _validate_thing(format_thing("i310_nodes", raw), "thing")
