"""Unit tests for #612: record ids and dict keys go through the CBOR checks.

``_preprocess_for_cbor()`` converts and checks values before cbor2 runs, but it
did not enter a ``RecordId``'s id or a dict key. So an integer outside the
64-bit range went out from there as a bignum (tag 2), which SurrealDB rejects
(HTTP 400, no reply over WebSocket), instead of raising ``ValueError`` before
the request as it does for a plain value. And a ``date`` inside a record id went
out as cbor2's native tag 1004, which SurrealDB does not read, where a plain
``date`` is sent as a UTC-midnight datetime (tag 12).

The tests compare the emitted bytes with the tags expected on the wire.
"""

from __future__ import annotations

import copy
import warnings
from datetime import UTC, date, datetime
from typing import Any

import cbor2
import pytest
from cbor2 import CBORTag

from surreal_sdk.protocol import cbor
from surreal_sdk.protocol.cbor import RecordId, encode

DAY = date(2026, 1, 2)
#: 2026-01-02T00:00:00Z as SurrealDB's compact datetime, tag 12 [seconds, nanoseconds].
DAY_TAG = CBORTag(12, [1_767_312_000, 0])
NAIVE = datetime(2026, 1, 2, 3, 4, 5, 6)
NAIVE_TAG = CBORTag(12, [1_767_323_045, 6_000])
BIG = 2**70
WARNING = "naive datetime"


def _plain_message(value: int) -> str:
    """The message ``encode()`` gives for this integer as a plain value."""
    with pytest.raises(ValueError) as raised:
        encode({"n": value})
    return str(raised.value)


def _rid(table: str, value: Any) -> CBORTag:
    """The record id as it must appear on the wire."""
    return CBORTag(8, [table, value])


class TestRecordIdIntegerRange:
    """An integer CBOR could only send as a bignum raises, inside a record id too."""

    @pytest.mark.parametrize(
        "record_id",
        [
            pytest.param(RecordId("t", [BIG]), id="array"),
            pytest.param(RecordId("t", {"k": BIG}), id="object_value"),
            pytest.param(RecordId("t", {BIG: "v"}), id="object_key"),
            pytest.param(RecordId("t", BIG), id="bare"),
            pytest.param(RecordId("t", ["a", ("b", [BIG])]), id="deep"),
            pytest.param(RecordId("t", [RecordId("u", [BIG])]), id="nested_record_id"),
            pytest.param(RecordId("t", RecordId("u", {"k": BIG})), id="record_id_as_id"),
        ],
    )
    def test_bignum_raises_with_the_plain_value_message(self, record_id: RecordId) -> None:
        with pytest.raises(ValueError) as raised:
            encode({"id": record_id})
        assert str(raised.value) == _plain_message(BIG)

    @pytest.mark.parametrize("value", [2**64, -(2**64) - 1, -(2**70)])
    def test_each_side_of_the_range_raises(self, value: int) -> None:
        with pytest.raises(ValueError) as raised:
            encode(RecordId("t", [value]))
        assert str(raised.value) == _plain_message(value)

    @pytest.mark.parametrize("value", [0, 1, -1, 2**63 - 1, -(2**63), 2**63, 2**64 - 1, -(2**64)])
    def test_64_bit_values_encode_as_before(self, value: int) -> None:
        record_id = RecordId("t", [value, {"k": value}])
        assert encode(record_id) == cbor2.dumps(_rid("t", [value, {"k": value}]))

    def test_bool_is_not_an_integer_here(self) -> None:
        assert encode(RecordId("t", [True, False])) == cbor2.dumps(_rid("t", [True, False]))


class TestDictKeyIntegerRange:
    """A dict key is range-checked like a value."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param({BIG: 1}, id="top_level"),
            pytest.param([{BIG: 1}], id="in_a_list"),
            pytest.param({"a": {BIG: 1}}, id="in_a_dict_value"),
            pytest.param({"a": [({BIG: 1},)]}, id="deep"),
            pytest.param({(1, BIG): 1}, id="inside_a_tuple_key"),
        ],
    )
    def test_bignum_key_raises_with_the_plain_value_message(self, value: Any) -> None:
        with pytest.raises(ValueError) as raised:
            encode(value)
        assert str(raised.value) == _plain_message(BIG)

    @pytest.mark.parametrize("value", [2**64, -(2**64) - 1])
    def test_each_side_of_the_range_raises(self, value: int) -> None:
        with pytest.raises(ValueError) as raised:
            encode({value: 1})
        assert str(raised.value) == _plain_message(value)

    def test_64_bit_keys_encode_as_before(self) -> None:
        value = {0: "a", 2**63: "b", 2**64 - 1: "c", -(2**64): "d", (1, 2**64 - 1): "e", "s": "f"}
        assert encode(value) == cbor2.dumps(value)

    def test_keys_are_only_checked_not_converted(self) -> None:
        # A date key keeps cbor2's encoding: only integer keys are checked.
        assert encode({DAY: 1}) == cbor2.dumps({DAY: 1})


class TestDateInRecordId:
    """A ``date`` inside a record id goes out as tag 12, like anywhere else."""

    def test_array_element_is_tag_12(self) -> None:
        assert encode(RecordId("t", [DAY])) == cbor2.dumps(_rid("t", [DAY_TAG]))

    def test_bare_date_id_is_tag_12(self) -> None:
        assert encode(RecordId("t", DAY)) == cbor2.dumps(_rid("t", DAY_TAG))

    def test_at_any_depth(self) -> None:
        record_id = RecordId("t", {"a": [("x", DAY)], "b": RecordId("u", [DAY])})
        expected = {"id": _rid("t", {"a": [["x", DAY_TAG]], "b": _rid("u", [DAY_TAG])})}
        assert encode({"id": record_id}) == cbor2.dumps(expected)

    def test_bytes_match_a_utc_midnight_datetime(self) -> None:
        midnight = datetime(2026, 1, 2, tzinfo=UTC)
        assert encode(RecordId("t", ["k", DAY])) == encode(RecordId("t", ["k", midnight]))

    def test_shared_container_is_converted_everywhere(self) -> None:
        shared = [DAY]
        expected = _rid("t", [[DAY_TAG], {"s": [DAY_TAG]}])
        assert encode(RecordId("t", [shared, {"s": shared}])) == cbor2.dumps(expected)


class TestDatetimeInRecordIdUnchanged:
    """Datetimes inside a record id encode exactly as before."""

    def test_aware_datetime(self) -> None:
        aware = NAIVE.replace(tzinfo=UTC)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            assert encode(RecordId("t", [aware])) == cbor2.dumps(_rid("t", [NAIVE_TAG]))

    def test_naive_datetime_warns_once_per_record_id(self) -> None:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", RuntimeWarning)
            encoded = encode(RecordId("t", [NAIVE, {"k": NAIVE}, RecordId("u", [NAIVE])]))
        assert [str(w.message) for w in caught if WARNING in str(w.message)] == [cbor._NAIVE_DATETIME_WARNING]
        assert encoded == cbor2.dumps(_rid("t", [NAIVE_TAG, {"k": NAIVE_TAG}, _rid("u", [NAIVE_TAG])]))


class TestCallerRecordIdNotMutated:
    def test_converted_copy_leaves_the_caller_object_alone(self) -> None:
        inner = {"k": [DAY]}
        nested = RecordId("u", [DAY])
        record_id = RecordId("t", [DAY, inner, nested])
        before = copy.deepcopy(record_id)
        original_list = record_id.id

        encode({"id": record_id})

        assert record_id == before
        assert record_id.id is original_list
        assert record_id.id[1] is inner and inner["k"] == [DAY]
        assert record_id.id[2] is nested and nested.id == [DAY]

    def test_record_id_with_nothing_to_convert_is_returned_as_is(self) -> None:
        record_id = RecordId("t", ["x", 1, {"k": None}])
        assert cbor._preprocess_for_cbor(record_id) is record_id


class TestCyclicRecordIdStillReachesCbor2:
    def test_cycle_holding_a_date_is_cbor2s_error(self) -> None:
        cyclic: list[Any] = [DAY]
        cyclic.append(cyclic)
        with pytest.raises(cbor2.CBOREncodeValueError, match="cyclic data structure"):
            encode({"id": RecordId("t", cyclic)})

    def test_cycle_through_a_nested_record_id(self) -> None:
        cyclic: list[Any] = [DAY]
        cyclic.append(RecordId("u", cyclic))
        with pytest.raises(cbor2.CBOREncodeValueError, match="cyclic data structure"):
            encode(RecordId("t", cyclic))
