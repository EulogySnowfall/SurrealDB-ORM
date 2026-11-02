"""Unit tests for the JSON Lines value codec (``surreal_sdk.jsonl``), U1-U5.

The codec is the only JSON <-> value frontier of the data export/import: every
value the SDK decodes from a SurrealDB reply must come back equal after a trip
through one JSON line, and every value it cannot carry losslessly must be
refused at export instead of being written as something else.
"""

from __future__ import annotations

import json
import math
import warnings
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import cbor2
import pytest

from surreal_sdk.exceptions import DataFormatError, DataImportError, QueryError, SurrealDBError
from surreal_sdk.jsonl import JSONL_TAGS, ImportReport, decode_value, dumps_record, encode_value, loads_record
from surreal_sdk.protocol.cbor import Duration, RecordId, Table
from surreal_sdk.utils import escape_record_id, format_record_id, validate_table_name

UID = UUID("0190f3b2-7c1e-7a4b-9f00-123456789abc")
GEOMETRY_TEXT = "a tuple is how the SDK decodes a geometry, a range or an out-of-range datetime"


def _frozen_mapping_type() -> type[Any]:
    """The type cbor2 decodes a map used as a map key to (an object record id key, M22).

    It is ``cbor2.frozendict`` up to Python 3.14 and the built-in ``frozendict``
    from Python 3.15 on, where cbor2 no longer defines its own: take it from a
    decode instead of naming either.
    """
    decoded = cbor2.loads(bytes.fromhex("a1a1616b0101"))  # {{"k": 1}: 1}
    return type(next(iter(decoded)))


FROZENDICT = _frozen_mapping_type()


def round_trip(value: Any) -> Any:
    """Encode, go through JSON text, decode."""
    return decode_value(json.loads(json.dumps(encode_value(value), allow_nan=False)))


# ---------------------------------------------------------------------------
# U1 — round trip of every row of the spec table
# ---------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize(
        "value",
        [
            True,
            False,
            "text",
            "",
            0,
            -12,
            2**70,
            1.5,
            -0.25,
            Decimal("1.10"),
            Decimal("-1E+5"),
            UID,
            Duration("1h30m"),
            Duration((5400,)),
            Duration((5400, 1)),
            Duration((0, 5)),
            Duration(()),
            b"\x00\x01binary\xff",
            Table("person"),
            RecordId("person", "alice"),
            RecordId("person", "7 x`\\"),
            RecordId("person", 42),
            RecordId("person", UID),
            ["a", 1, None, [Decimal("2.5")]],
            {"a": {"b": [1, {"c": UID}]}, "n": None},
        ],
    )
    def test_value_round_trips(self, value: Any) -> None:
        assert round_trip(value) == value

    def test_none_is_null(self) -> None:
        assert encode_value(None) is None
        assert round_trip(None) is None

    def test_bytearray_comes_back_as_bytes(self) -> None:
        assert round_trip(bytearray(b"ab")) == b"ab"
        assert encode_value(b"ab") == {"$bytes": "YWI="}

    @pytest.mark.parametrize(
        ("value", "tag"),
        [(math.nan, "NaN"), (math.inf, "Infinity"), (-math.inf, "-Infinity")],
    )
    def test_non_finite_floats(self, value: float, tag: str) -> None:
        assert encode_value(value) == {"$float": tag}
        decoded = round_trip(value)
        assert isinstance(decoded, float)
        if math.isnan(value):
            assert math.isnan(decoded)
        else:
            assert decoded == value

    def test_tagged_shapes(self) -> None:
        assert encode_value(Decimal("1.10")) == {"$decimal": "1.10"}
        assert encode_value(UID) == {"$uuid": str(UID)}
        assert encode_value(Table("x")) == {"$table": "x"}
        assert encode_value(Duration("1h30m")) == {"$duration": "1h30m"}
        assert encode_value(Duration((5400,))) == {"$duration": [5400]}
        assert encode_value(Duration((5400, 1))) == {"$duration": [5400, 1]}
        assert encode_value(Duration(())) == {"$duration": []}
        assert encode_value(RecordId("person", "alice")) == {"$record": ["person", "alice"]}
        assert encode_value(RecordId("person", UID)) == {"$record": ["person", {"$uuid": str(UID)}]}

    def test_duration_decodes_to_its_form(self) -> None:
        assert decode_value({"$duration": "1h30m"}) == Duration("1h30m")
        assert decode_value({"$duration": [5400]}) == Duration((5400,))
        assert decode_value({"$duration": []}) == Duration(())

    def test_aware_datetime_is_utc_with_six_digits(self) -> None:
        value = datetime(2020, 1, 2, 4, 4, 5, 123456, tzinfo=timezone(timedelta(hours=1)))
        assert encode_value(value) == {"$datetime": "2020-01-02T03:04:05.123456Z"}
        decoded = round_trip(value)
        assert decoded == value
        assert decoded.tzinfo == UTC
        assert encode_value(datetime(2020, 1, 2, tzinfo=UTC)) == {"$datetime": "2020-01-02T00:00:00.000000Z"}

    def test_early_year_is_zero_padded(self) -> None:
        value = datetime(5, 1, 2, tzinfo=UTC)
        assert encode_value(value) == {"$datetime": "0005-01-02T00:00:00.000000Z"}
        assert round_trip(value) == value

    def test_naive_datetime_is_read_as_utc_with_a_warning(self) -> None:
        with pytest.warns(RuntimeWarning, match="naive datetime"):
            encoded = encode_value(datetime(2020, 1, 2, 3, 4, 5))
        assert encoded == {"$datetime": "2020-01-02T03:04:05.000000Z"}

    def test_date_is_utc_midnight(self) -> None:
        assert encode_value(date(2020, 1, 2)) == {"$datetime": "2020-01-02T00:00:00.000000Z"}
        assert round_trip(date(2020, 1, 2)) == datetime(2020, 1, 2, tzinfo=UTC)

    def test_record_id_with_tuple_id_comes_back_as_list(self) -> None:
        value = RecordId("person", (1, "a", (2, UID)))
        assert encode_value(value) == {"$record": ["person", [1, "a", [2, {"$uuid": str(UID)}]]]}
        assert round_trip(value) == RecordId("person", [1, "a", [2, UID]])

    def test_record_id_with_frozendict_id_comes_back_as_dict(self) -> None:
        value = RecordId("person", FROZENDICT({"k": 1, "u": UID}))
        decoded = round_trip(value)
        assert decoded == RecordId("person", {"k": 1, "u": UID})
        assert type(decoded.id) is dict

    def test_dollar_key_object_is_wrapped(self) -> None:
        assert encode_value({"$weird": 1}) == {"$object": {"$weird": 1}}
        assert round_trip({"$weird": 1}) == {"$weird": 1}
        assert round_trip({"$record": "not a tag"}) == {"$record": "not a tag"}
        # Two keys cannot be read as a tag: no wrapping.
        assert encode_value({"$a": 1, "$b": 2}) == {"$a": 1, "$b": 2}

    def test_object_payload_values_are_decoded_but_not_tag_checked(self) -> None:
        assert decode_value({"$object": {"$uuid": {"$uuid": str(UID)}}}) == {"$uuid": UID}

    def test_frozendict_value_is_an_object(self) -> None:
        assert round_trip({"o": FROZENDICT({"a": 1})}) == {"o": {"a": 1}}

    def test_tags_constant(self) -> None:
        assert (
            frozenset({"$record", "$table", "$datetime", "$decimal", "$uuid", "$duration", "$bytes", "$float", "$object"})
            == JSONL_TAGS
        )

    def test_loads_record_round_trips_a_record(self) -> None:
        record = {
            "id": RecordId("t", "a"),
            "when": datetime(2020, 1, 2, tzinfo=UTC),
            "price": Decimal("9.99"),
            "list": [1, Duration((3,))],
        }
        assert loads_record(dumps_record(record)) == record


# ---------------------------------------------------------------------------
# U2 — E2: values that would not round-trip are refused at export
# ---------------------------------------------------------------------------


class TestExportRefusals:
    def test_tuple_at_top_level_of_a_record(self) -> None:
        with pytest.raises(DataFormatError) as info:
            dumps_record({"id": RecordId("t", "a"), "loc": (1.0, 2.0)})
        assert str(info.value) == (
            f"Cannot write loc of t:a as JSON Lines: {GEOMETRY_TEXT}, so it would not round-trip; "
            "use dump_database() for a lossless copy"
        )
        assert info.value.path == "loc"
        assert info.value.line is None

    def test_nested_tuple_path(self) -> None:
        with pytest.raises(DataFormatError, match=r"^Cannot write nested\.arr\[2\] of t:a as JSON Lines: a tuple"):
            dumps_record({"id": RecordId("t", "a"), "nested": {"arr": [1, 2, (3, 4)]}})

    def test_tuple_in_a_list(self) -> None:
        with pytest.raises(DataFormatError, match=r"^Cannot write items\[0\] of a record without id as JSON Lines"):
            dumps_record({"items": [(1, 2)]})

    def test_set_is_unsupported(self) -> None:
        with pytest.raises(DataFormatError) as info:
            dumps_record({"id": RecordId("t", 1), "s": {1, 2}})
        assert str(info.value) == "Cannot write s of t:1 as JSON Lines: unsupported value of type set"

    def test_non_finite_decimal_is_unsupported(self) -> None:
        with pytest.raises(DataFormatError, match="unsupported value of type Decimal"):
            dumps_record({"d": Decimal("NaN")})

    def test_arbitrary_object_is_unsupported(self) -> None:
        with pytest.raises(DataFormatError, match="unsupported value of type object"):
            encode_value(object())

    def test_non_string_key_is_unsupported(self) -> None:
        with pytest.raises(DataFormatError, match="unsupported"):
            dumps_record({"m": {1: "a"}})

    @pytest.mark.parametrize("bad", [1.5, (1, 2, 3), (-1,), (True,), ("1",)])
    def test_duration_with_an_unexpected_value(self, bad: Any) -> None:
        with pytest.raises(DataFormatError, match="unsupported value of type Duration"):
            encode_value(Duration(bad))
        with pytest.raises(DataFormatError, match="unsupported value of type Duration"):
            encode_value(Duration(bad), typed=False)

    def test_tuple_inside_a_record_id_is_fine(self) -> None:
        line = dumps_record({"id": RecordId("t", ("a", 1))})
        assert line == '{"id":{"$record":["t",["a",1]]}}'

    def test_is_a_value_error_and_an_sdk_error(self) -> None:
        assert issubclass(DataFormatError, ValueError)
        assert issubclass(DataFormatError, SurrealDBError)


# ---------------------------------------------------------------------------
# U3 — E3: malformed input lines
# ---------------------------------------------------------------------------


class TestDecodeErrors:
    @pytest.mark.parametrize(
        "obj",
        [
            {"$record": ["t"]},
            {"$record": "t:a"},
            {"$record": [1, "a"]},
            {"$record": ["t", True]},
            {"$record": ["t", None]},
            {"$record": ["t", 1.5]},
            {"$table": 3},
            {"$datetime": "yesterday"},
            {"$datetime": 0},
            {"$decimal": "abc"},
            {"$decimal": "NaN"},
            {"$decimal": 1.5},
            {"$uuid": "nope"},
            {"$uuid": 1},
            {"$bytes": "!!not base64"},
            {"$bytes": 1},
            {"$float": "x"},
            {"$float": 1.0},
            {"$object": [1]},
            {"$duration": "zz"},
            {"$duration": "1h "},
            {"$duration": "-1s"},
            {"$duration": ""},
            {"$duration": [1, 2, 3]},
            {"$duration": [-1]},
            {"$duration": [True]},
            {"$duration": 5},
        ],
    )
    def test_malformed_tag_payload(self, obj: dict[str, Any]) -> None:
        (tag, payload) = next(iter(obj.items()))
        with pytest.raises(DataFormatError) as info:
            decode_value(obj)
        assert str(info.value) == f"invalid {tag} value: {payload!r}"

    @pytest.mark.parametrize("text", ["01h", "1µs", "1us", "2w3d4h5m6s7ms8us9ns", "1y"])
    def test_duration_grammar_accepts(self, text: str) -> None:
        assert decode_value({"$duration": text}) == Duration(text)

    def test_unknown_tag(self) -> None:
        with pytest.raises(DataFormatError) as info:
            decode_value({"$recrod": ["t", "a"]})
        assert str(info.value) == "unknown tag '$recrod'"

    def test_nested_error_carries_the_line(self) -> None:
        with pytest.raises(DataFormatError) as info:
            decode_value({"a": [{"$float": "x"}]}, _line=7)
        assert str(info.value) == "Line 7: invalid $float value: 'x'"
        assert info.value.line == 7

    def test_line_prefix_from_loads_record(self) -> None:
        with pytest.raises(DataFormatError, match=r"^Line 7: unknown tag '\$x'$"):
            loads_record('{"a": {"$x": 1}}', _line=7)

    @pytest.mark.parametrize(
        ("line", "kind"),
        [("[1, 2]", "array"), ('"a"', "string"), ("1", "number"), ("null", "null"), ("true", "boolean")],
    )
    def test_non_object_line(self, line: str, kind: str) -> None:
        with pytest.raises(DataFormatError) as info:
            loads_record(line, _line=3)
        assert str(info.value) == f"Line 3: expected a JSON object, got {kind}"

    @pytest.mark.parametrize("line", ["{", '{"a": NaN}', '{"a": Infinity}', ""])
    def test_invalid_json(self, line: str) -> None:
        with pytest.raises(DataFormatError, match=r"^Line 2: invalid JSON \(.+\)$"):
            loads_record(line, _line=2)

    def test_invalid_json_without_line_number(self) -> None:
        with pytest.raises(DataFormatError, match=r"^invalid JSON \("):
            loads_record("{")

    def test_bom_is_stripped(self) -> None:
        assert loads_record('﻿{"a": 1}') == {"a": 1}

    def test_tag_is_only_a_one_key_object(self) -> None:
        assert decode_value({"$uuid": str(UID), "x": 1}) == {"$uuid": str(UID), "x": 1}


# ---------------------------------------------------------------------------
# U4 — relaxed encoding (typed=False)
# ---------------------------------------------------------------------------


class TestRelaxedEncoding:
    def test_each_type_as_a_plain_value(self) -> None:
        record = {
            "id": RecordId("person", "alice"),
            "t": Table("x"),
            "when": datetime(2020, 1, 2, 3, 4, 5, 123456, tzinfo=UTC),
            "price": Decimal("1.10"),
            "u": UID,
            "d1": Duration("1h30m"),
            "d2": Duration((5400, 1)),
            "d3": Duration(()),
            "b": b"ab",
            "nan": math.nan,
            "inf": math.inf,
            "ninf": -math.inf,
            "weird": {"$x": 1},
        }
        assert json.loads(dumps_record(record, typed=False)) == {
            "id": "person:alice",
            "t": "x",
            "when": "2020-01-02T03:04:05.123456Z",
            "price": "1.10",
            "u": str(UID),
            "d1": "1h30m",
            "d2": "5400s1ns",
            "d3": "0ns",
            "b": "YWI=",
            "nan": "NaN",
            "inf": "Infinity",
            "ninf": "-Infinity",
            "weird": {"$x": 1},
        }

    @pytest.mark.parametrize(
        ("rid", "text"),
        [
            (RecordId("person", "alice"), "person:alice"),
            (RecordId("person", "7 x"), "person:`7 x`"),
            (RecordId("person", "a`b\\"), "person:`a\\`b\\\\`"),
            (RecordId("person", 42), "person:42"),
            (RecordId("person", UID), f"person:u'{UID}'"),
            (RecordId("person", [1, "a"]), 'person:[1,"a"]'),
            (RecordId("person", (1, "a")), 'person:[1,"a"]'),
            (RecordId("person", {"k": "é"}), 'person:{"k":"é"}'),
        ],
    )
    def test_format_record_id(self, rid: RecordId, text: str) -> None:
        assert format_record_id(rid) == text
        assert encode_value(rid, typed=False) == text

    def test_tuples_are_still_refused(self) -> None:
        with pytest.raises(DataFormatError, match="a tuple"):
            dumps_record({"loc": (1, 2)}, typed=False)

    def test_relaxed_file_reads_back_as_plain_json(self) -> None:
        line = dumps_record({"id": RecordId("t", "a"), "d": Decimal("2")}, typed=False)
        assert loads_record(line) == {"id": "t:a", "d": "2"}


# ---------------------------------------------------------------------------
# U5 — line shape
# ---------------------------------------------------------------------------


class TestLineShape:
    def test_one_line_even_with_newlines_in_values(self) -> None:
        line = dumps_record({"text": "a\nb\r\nc", "nested": {"k": "x\ny"}})
        assert "\n" not in line
        assert "\r" not in line
        assert loads_record(line) == {"text": "a\nb\r\nc", "nested": {"k": "x\ny"}}

    def test_unicode_line_separators_are_escaped(self) -> None:
        value = {"text": "a\u2028b\u2029c\x85d"}
        line = dumps_record(value)
        assert len(line.splitlines()) == 1
        assert loads_record(line) == value

    def test_non_ascii_is_kept(self) -> None:
        line = dumps_record({"name": "café", "emoji": "\U0001f600"})
        assert "café" in line
        assert "\U0001f600" in line

    def test_compact_separators(self) -> None:
        assert dumps_record({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'

    def test_no_warning_for_aware_values(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            dumps_record({"w": datetime(2020, 1, 1, tzinfo=UTC)})


# ---------------------------------------------------------------------------
# Exceptions, helpers and P2 (one record-id writer)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_data_import_error(self) -> None:
        report = ImportReport(table="t", lines=3, rows=2, batches=1)
        error = DataImportError("boom", errors=["first"], code=400, report=report)
        assert isinstance(error, QueryError)
        assert str(error) == "boom"
        assert error.errors == ["first"]
        assert error.code == 400
        assert error.report is report
        plain = DataImportError("x", errors=[])
        assert plain.code is None
        assert plain.report is None

    def test_validate_table_name(self) -> None:
        assert validate_table_name("person_2") == "person_2"
        for bad in ["t y", "t\n", "", "1t", "t-x", "é"]:
            with pytest.raises(ValueError) as info:
                validate_table_name(bad)
            assert str(info.value) == f"Invalid table name: {bad!r} (expected an identifier)"

    def test_orm_escape_helpers_delegate_to_the_sdk(self) -> None:
        from src.surreal_orm import utils as orm_utils

        assert orm_utils.escape_record_id is escape_record_id
        assert orm_utils.format_thing("users", "7abc") == "users:`7abc`"
