"""Unit tests for #578: datetimes and durations go out as CBOR tags 12 and 14.

The SDK sent a bound ``datetime`` as the standard CBOR tag 0 (an ISO 8601
string), a ``date`` as the same tag once turned into a UTC-midnight
``datetime``, and a text ``Duration`` as tag 13 (a duration string).
SurrealDB 3.x reads those, but 4.0 refuses them and asks for the compact
forms, which every server since 1.5 reads: tag 12 ``[seconds, nanoseconds]``
and tag 14 ``[seconds, nanoseconds]``. The tests decode the bytes without the
SDK's tag hook, so they see the tags actually on the wire.
"""

from __future__ import annotations

import uuid
import warnings
from datetime import UTC, date, datetime, timedelta, timezone

import cbor2
import pytest
from cbor2 import CBORTag

from surreal_sdk.protocol.cbor import Duration, RecordId, decode, encode

PLUS_TWO = timezone(timedelta(hours=2))
#: Tags SurrealDB 4.0 refuses: an ISO datetime, a UUID string, a duration string.
REFUSED_TAGS = {0, 9, 13}


class I578MyDT(datetime):
    """A ``datetime`` subclass, as ``pandas.Timestamp`` or ``pendulum.DateTime`` are."""


class I578MyUUID(uuid.UUID):
    """A ``UUID`` subclass, as ``asyncpg.pgproto.pgproto.UUID`` is."""


UUID_TEXT = "12345678-1234-5678-1234-567812345678"


def _raw(value: object) -> object:
    """Decode without the SDK tag hook, warnings silenced (naive datetimes warn)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return cbor2.loads(encode(value))


def _tags(value: object) -> set[int]:
    """Every CBOR tag number found anywhere in the decoded value."""
    found: set[int] = set()

    def walk(item: object) -> None:
        if isinstance(item, CBORTag):
            found.add(item.tag)
            walk(item.value)
        elif isinstance(item, dict):
            for key, val in item.items():
                walk(key)
                walk(val)
        elif isinstance(item, (list, tuple)):
            for sub in item:
                walk(sub)

    walk(value)
    return found


def _pair(raw: object) -> tuple[int, int]:
    assert isinstance(raw, CBORTag), raw
    assert raw.tag == 12, raw
    seconds, nanoseconds = raw.value
    return seconds, nanoseconds


class TestDatetimeIsTag12:
    """Every ``datetime`` is the ``[seconds, nanoseconds]`` pair under tag 12."""

    def test_aware_utc(self) -> None:
        assert _pair(_raw(datetime(2025, 2, 13, 5, 0, tzinfo=UTC))) == (1739422800, 0)

    def test_aware_other_offset_is_the_same_instant(self) -> None:
        assert _pair(_raw(datetime(2025, 2, 13, 7, 0, tzinfo=PLUS_TWO))) == (1739422800, 0)

    def test_naive_is_read_as_utc(self) -> None:
        assert _pair(_raw(datetime(2025, 2, 13, 5, 0))) == (1739422800, 0)

    def test_naive_still_warns(self) -> None:
        with pytest.warns(RuntimeWarning, match="A naive datetime was sent to SurrealDB and read as UTC"):
            encode({"d": datetime(2025, 2, 13, 5, 0)})

    def test_microseconds_become_nanoseconds(self) -> None:
        assert _pair(_raw(datetime(2025, 2, 13, 5, 0, 0, 123456, tzinfo=UTC))) == (1739422800, 123_456_000)

    def test_before_1970_floors_the_seconds(self) -> None:
        """1969-12-31T23:59:59.5Z is -0.5 s: seconds -1, nanoseconds 500 000 000."""
        assert _pair(_raw(datetime(1969, 12, 31, 23, 59, 59, 500000, tzinfo=UTC))) == (-1, 500_000_000)

    def test_far_past(self) -> None:
        assert _pair(_raw(datetime(1, 1, 1, tzinfo=UTC))) == (-62135596800, 0)

    def test_date_is_utc_midnight(self) -> None:
        assert _pair(_raw(date(2026, 2, 1))) == (1769904000, 0)

    def test_subclass(self) -> None:
        assert _pair(_raw(I578MyDT(2025, 2, 13, 5, 0, 0, 7, tzinfo=UTC))) == (1739422800, 7000)

    def test_naive_subclass(self) -> None:
        assert _pair(_raw(I578MyDT(2025, 2, 13, 5, 0))) == (1739422800, 0)

    def test_inside_a_record_id_key(self) -> None:
        raw = _raw(RecordId("t", ["a", datetime(2025, 2, 13, 5, 0, tzinfo=UTC), datetime(2025, 2, 13, 5, 0)]))
        assert isinstance(raw, CBORTag) and raw.tag == 8
        _table, key = raw.value
        assert key[0] == "a"
        assert _pair(key[1]) == _pair(key[2]) == (1739422800, 0)

    def test_nested(self) -> None:
        raw = _raw({"a": [{"b": datetime(2025, 2, 13, 5, 0, tzinfo=UTC)}], "c": (date(2026, 2, 1),)})
        assert isinstance(raw, dict)
        assert _pair(raw["a"][0]["b"]) == (1739422800, 0)
        assert _pair(raw["c"][0]) == (1769904000, 0)

    @pytest.mark.parametrize(
        "value",
        [
            datetime(2025, 2, 13, 5, 0, 0, 123456, tzinfo=UTC),
            datetime(1969, 12, 31, 23, 59, 59, 1, tzinfo=UTC),
            datetime(1, 1, 1, tzinfo=UTC),
            datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        ],
    )
    def test_round_trip(self, value: datetime) -> None:
        assert decode(encode(value)) == value

    def test_the_value_passed_in_is_not_modified(self) -> None:
        payload = {"d": datetime(2025, 2, 13, 5, 0)}
        _raw(payload)
        assert payload["d"].tzinfo is None


class TestTextDurationIsTag14:
    """A text ``Duration`` is parsed and sent as tag 14, never as tag 13."""

    @pytest.mark.parametrize(
        ("text", "payload"),
        [
            ("1h30m", [5400]),
            ("90m", [5400]),
            ("30m1h", [5400]),
            ("1h1h", [7200]),
            ("01h", [3600]),
            ("0ns", []),
            ("1ns", [0, 1]),
            ("1us", [0, 1000]),
            ("1\u00b5s", [0, 1000]),
            ("1ms", [0, 1_000_000]),
            ("1s", [1]),
            ("1m", [60]),
            ("1d", [86400]),
            ("1w", [604800]),
            ("1y", [31536000]),
            ("1000000000ns", [1]),
            ("1y2w3d4h5m6s7ms8us9ns", [33019506, 7008009]),
            ("18446744073709551615s999999999ns", [18446744073709551615, 999999999]),
        ],
    )
    def test_payload(self, text: str, payload: list[int]) -> None:
        raw = _raw(Duration(text))
        assert isinstance(raw, CBORTag)
        assert raw.tag == 14
        assert list(raw.value) == payload

    def test_nested_and_in_record_id(self) -> None:
        raw = _raw({"a": [Duration("2h")], "r": RecordId("t", [Duration("5ns")])})
        assert _tags(raw) == {8, 14}

    def test_decodes_as_the_server_would_return_it(self) -> None:
        assert decode(encode(Duration("1h30m"))) == Duration((5400,))

    @pytest.mark.parametrize(
        "text",
        ["", "abc", "1h 30m", "1.5h", "-1h", "1H", "1\u03bcs", "h", "1", "1h30", "1hh", "18446744073709551616s"],
    )
    def test_invalid_text_raises_value_error(self, text: str) -> None:
        with pytest.raises(ValueError, match="duration"):
            encode({"d": Duration(text)})

    def test_non_ascii_digit_raises_value_error(self) -> None:
        """``\\d`` and ``str.isdigit()`` accept other scripts' digits; the server does not."""
        with pytest.raises(ValueError, match="duration"):
            encode(Duration(chr(0x0661) + "h"))

    def test_invalid_text_in_record_id_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            encode(RecordId("t", [Duration("nope")]))


class TestNoRefusedTag:
    """The 4.0-refused tags 0, 9 and 13 never appear for any of these values."""

    def test_mixed_payload(self) -> None:
        payload = {
            "aware": datetime(2025, 2, 13, 5, 0, tzinfo=UTC),
            "naive": datetime(2025, 2, 13, 5, 0),
            "micro": datetime(2025, 2, 13, 5, 0, 0, 1, tzinfo=PLUS_TWO),
            "old": datetime(1900, 1, 1, tzinfo=UTC),
            "day": date(2026, 2, 1),
            "sub": I578MyDT(2025, 2, 13, tzinfo=UTC),
            "rid": RecordId("t", [datetime(2025, 2, 13, tzinfo=UTC)]),
            "dur": Duration("1h30m"),
            "uuid": I578MyUUID(UUID_TEXT),
        }
        assert not _tags(_raw(payload)) & REFUSED_TAGS


class TestUuidIsTag37:
    """A UUID goes out as the standard tag 37, a subclass included, never tag 9.

    cbor2 encodes a plain ``uuid.UUID`` natively but looks encoders up by exact
    type, so a subclass reaches the SDK's default hook.
    """

    def test_plain_uuid(self) -> None:
        raw = _raw(uuid.UUID(UUID_TEXT))
        assert raw == uuid.UUID(UUID_TEXT)

    def test_subclass_is_sent_as_tag_37(self) -> None:
        data = encode(I578MyUUID(UUID_TEXT))
        assert data == encode(uuid.UUID(UUID_TEXT))
        # Tag 37 (0xd8 0x25) over a 16-byte string (0x50).
        assert data == b"\xd8\x25\x50" + uuid.UUID(UUID_TEXT).bytes

    def test_subclass_nested_and_in_record_id(self) -> None:
        raw = _raw({"u": [I578MyUUID(UUID_TEXT)], "r": RecordId("t", [I578MyUUID(UUID_TEXT)])})
        assert not _tags(raw) & REFUSED_TAGS
        assert raw["u"] == [uuid.UUID(UUID_TEXT)]

    def test_subclass_round_trips_as_a_plain_uuid(self) -> None:
        value = decode(encode(I578MyUUID(UUID_TEXT)))
        assert type(value) is uuid.UUID
        assert value == uuid.UUID(UUID_TEXT)


class TestOutOfRangeInteger:
    """An integer CBOR can only carry as a bignum is refused before any request.

    SurrealDB 3.3.5 rejects such a bound value as a parse error (HTTP 400, no
    reply over WebSocket). Integers that fit in 64 bits are sent unchanged,
    including those between the signed and the unsigned 64-bit limits: 3.3.5
    accepts them (it wraps them into a signed 64-bit integer), so they keep
    the 3.3.5 result.
    """

    @pytest.mark.parametrize("value", [2**64, 2**64 + 5, -(2**64) - 1, -(2**70)])
    def test_bignum_raises_value_error(self, value: int) -> None:
        with pytest.raises(ValueError, match="64-bit"):
            encode({"n": [value]})

    @pytest.mark.parametrize("value", [0, 2**63 - 1, -(2**63), 2**64 - 1, -(2**64)])
    def test_64_bit_values_are_unchanged(self, value: int) -> None:
        assert encode({"n": value}) == cbor2.dumps({"n": value})

    def test_bool_is_not_an_integer_here(self) -> None:
        assert _raw([True, False]) == [True, False]
