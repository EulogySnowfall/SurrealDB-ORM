"""A ``Duration`` is sent with the CBOR tag that matches its value.

SurrealDB reads two duration tags: 13, a duration *string* (``"1h30m"``), and
14, the compact ``[seconds, nanoseconds]`` array, which is also the only form
the server sends. A string under tag 14 is not a duration to the server: over
HTTP the whole request fails with ``400 Parse error``, over WebSocket no reply
ever comes. The encoder picks the tag from the value's type; the decoder reads
both.
"""

import cbor2
import pytest
from cbor2 import CBORTag

from surreal_sdk.protocol.cbor import (
    TAG_CUSTOM_DURATION,
    TAG_STRING_DURATION,
    Duration,
    RecordId,
    decode,
    encode,
)


def _raw(value: object) -> object:
    """Decode without the SDK tag hook, to see the tags actually emitted."""
    return cbor2.loads(encode(value))


class TestDurationTags:
    def test_tag_numbers(self) -> None:
        assert TAG_STRING_DURATION == 13
        assert TAG_CUSTOM_DURATION == 14

    def test_string_duration_is_tag_13(self) -> None:
        assert _raw(Duration("1h30m")) == CBORTag(13, "1h30m")

    @pytest.mark.parametrize(
        ("value", "payload"),
        [((5400,), [5400]), ((5400, 1), [5400, 1]), ((), [])],
    )
    def test_compact_duration_is_tag_14_array(self, value: tuple[int, ...], payload: list[int]) -> None:
        raw = _raw(Duration(value))
        assert isinstance(raw, CBORTag)
        assert raw.tag == 14
        assert list(raw.value) == payload

    def test_nested_in_dict_and_list(self) -> None:
        raw = _raw({"a": Duration("2h"), "b": [Duration((1, 1)), Duration("5ns")]})
        assert isinstance(raw, dict)
        assert raw["a"] == CBORTag(13, "2h")
        assert raw["b"][0].tag == 14
        assert list(raw["b"][0].value) == [1, 1]
        assert raw["b"][1] == CBORTag(13, "5ns")

    def test_as_record_id_key_part(self) -> None:
        raw = _raw(RecordId("dwf_t", [Duration("1h"), Duration((3,))]))
        assert isinstance(raw, CBORTag)
        assert raw.tag == 8
        table, key = raw.value
        assert table == "dwf_t"
        assert key[0] == CBORTag(13, "1h")
        assert key[1].tag == 14
        assert list(key[1].value) == [3]


class TestDurationDecode:
    def test_decode_tag_13(self) -> None:
        assert decode(cbor2.dumps(CBORTag(13, "1h30m"))) == Duration("1h30m")

    def test_decode_tag_14_array(self) -> None:
        decoded = decode(cbor2.dumps(CBORTag(14, [5400, 1])))
        assert decoded == Duration((5400, 1))
        assert isinstance(decoded.value, tuple)

    def test_decode_tag_14_string_keeps_unchanged_branch(self) -> None:
        assert decode(cbor2.dumps(CBORTag(14, "1h"))) == Duration("1h")

    @pytest.mark.parametrize("value", ["1h30m", (5400,), (5400, 1), ()])
    def test_round_trip(self, value: str | tuple[int, ...]) -> None:
        assert decode(encode(Duration(value))) == Duration(value)


class TestDurationStr:
    @pytest.mark.parametrize(
        ("value", "text"),
        [
            ((), "0ns"),
            ((0, 0), "0ns"),
            ((5400,), "5400s"),
            ((0, 5), "5ns"),
            ((1, 1), "1s1ns"),
            ("1h30m", "1h30m"),
        ],
    )
    def test_str(self, value: str | tuple[int, ...], text: str) -> None:
        assert str(Duration(value)) == text
