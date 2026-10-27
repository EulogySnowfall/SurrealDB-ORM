"""Unit tests for #255: a naive datetime is sent over CBOR as UTC, with a warning.

cbor2 refuses to encode a ``datetime`` without a ``tzinfo`` unless it is told
which zone to assume, so every naive datetime bound over CBOR — the default
protocol — raised ``CBOREncodeError``: query variables, model fields on
``save()``, ``filter()`` and ``live()`` values. ``_cbor_default_encoder`` had a
branch meant to read it as UTC, but cbor2 encodes ``datetime`` natively and
never calls ``default`` for one, so that branch did not run for it.

It did run for a ``datetime`` *subclass* (``pandas.Timestamp``,
``pendulum.DateTime``), and sent it as tag 12 carrying an ISO string, a form
SurrealDB rejects as a parse error — aware or not.

A naive datetime is now read as UTC on every path, as
``raw_query(inline_dicts=True)`` already did, and a ``RuntimeWarning`` with a
constant message says so. Aware datetimes encode exactly as before.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta, timezone

import cbor2
import pytest

from surreal_orm.utils import _extract_datetime_values, inline_dict_variables
from surreal_sdk.protocol.cbor import RecordId, decode, encode

NAIVE = datetime(2026, 2, 1, 12, 30, 15, 123456)
AS_UTC = NAIVE.replace(tzinfo=UTC)
PLUS_TWO = timezone(timedelta(hours=2))

#: The message names neither the value nor the caller, so the default filter
#: shows it once per call site instead of once per distinct value.
WARNING = "A naive datetime was sent to SurrealDB and read as UTC"


class I255MyDT(datetime):
    """A ``datetime`` subclass, as ``pandas.Timestamp`` or ``pendulum.DateTime`` are."""


def _encode_quietly(value: object) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return encode(value)


def _no_warning_encode(value: object) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        return encode(value)


class TestNaiveDatetimeEncodesAsUtc:
    """``encode()`` of a naive datetime equals ``encode()`` of the same wall time in UTC."""

    @pytest.mark.parametrize(
        ("naive", "aware"),
        [
            pytest.param({"d": NAIVE}, {"d": AS_UTC}, id="top_level"),
            pytest.param({"d": [NAIVE, 1]}, {"d": [AS_UTC, 1]}, id="in_list"),
            pytest.param({"d": {"x": {"y": NAIVE}}}, {"d": {"x": {"y": AS_UTC}}}, id="in_dict"),
            pytest.param({"d": (NAIVE,)}, {"d": (AS_UTC,)}, id="in_tuple"),
            pytest.param({"d": RecordId("t", ["a", NAIVE])}, {"d": RecordId("t", ["a", AS_UTC])}, id="in_record_id"),
        ],
    )
    def test_same_bytes_as_utc(self, naive: object, aware: object) -> None:
        assert _encode_quietly(naive) == _no_warning_encode(aware)

    def test_decodes_as_the_utc_instant(self) -> None:
        assert decode(_encode_quietly({"d": NAIVE})) == {"d": AS_UTC}

    def test_the_value_passed_in_is_not_modified(self) -> None:
        payload = {"d": NAIVE}
        _encode_quietly(payload)
        assert payload["d"].tzinfo is None


class TestAwareDatetimeUnchanged:
    """An aware datetime gives exactly the bytes cbor2 gave it before."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(AS_UTC, id="utc"),
            pytest.param(datetime(2026, 2, 1, 12, 30, tzinfo=PLUS_TWO), id="plus_two"),
            pytest.param([datetime(2026, 2, 1, 12, 30, tzinfo=PLUS_TWO)], id="nested"),
        ],
    )
    def test_same_bytes_as_plain_cbor2(self, value: object) -> None:
        assert _no_warning_encode(value) == cbor2.dumps(value)


class TestRuntimeWarning:
    """A naive datetime warns, Django-style: one constant message, once per call site."""

    def test_cbor_path_warns(self) -> None:
        with pytest.warns(RuntimeWarning, match=WARNING):
            encode({"d": NAIVE})

    def test_nested_value_warns(self) -> None:
        with pytest.warns(RuntimeWarning, match=WARNING):
            encode({"d": [{"x": NAIVE}]})

    def test_aware_value_does_not_warn(self) -> None:
        _no_warning_encode({"d": AS_UTC, "e": [datetime(2026, 2, 1, tzinfo=PLUS_TWO)]})

    def test_message_carries_no_value(self) -> None:
        with pytest.warns(RuntimeWarning) as record:
            encode({"d": NAIVE})
        message = str(record[0].message)
        assert "2026" not in message
        assert "UTC" in message and "tzinfo" in message

    def test_default_filter_shows_it_once(self) -> None:
        """Different values, same call site: the default action reports it once."""
        with warnings.catch_warnings(record=True) as record:
            warnings.simplefilter("default")
            encode({"d": NAIVE})
            encode({"d": datetime(2020, 1, 1)})
            encode([datetime(1999, 12, 31, 23, 59)])
        assert len([w for w in record if issubclass(w.category, RuntimeWarning)]) == 1

    def test_inline_path_warns_and_stays_utc(self) -> None:
        markers: dict[str, str] = {}
        with pytest.warns(RuntimeWarning, match=WARNING):
            _extract_datetime_values({"at": NAIVE}, markers, [0])
        assert markers == {"__SURQL_DT_0__": 'd"2026-02-01T12:30:15.123456+00:00"'}

    def test_raw_query_inline_dicts_warns(self) -> None:
        with pytest.warns(RuntimeWarning, match=WARNING):
            query, remaining = inline_dict_variables("UPDATE t SET data = $data;", {"data": {"at": [NAIVE]}})
        assert 'd"2026-02-01T12:30:15.123456+00:00"' in query
        assert remaining == {}

    def test_inline_path_aware_value_does_not_warn(self) -> None:
        markers: dict[str, str] = {}
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _extract_datetime_values({"at": AS_UTC}, markers, [0])


class TestDatetimeSubclass:
    """A ``datetime`` subclass is sent like the equivalent plain ``datetime``."""

    @pytest.mark.parametrize(
        ("subclass", "plain"),
        [
            pytest.param(I255MyDT(2026, 2, 1, 12, 30, 15, 123456), NAIVE, id="naive"),
            pytest.param(I255MyDT(2026, 2, 1, 12, 30, tzinfo=UTC), datetime(2026, 2, 1, 12, 30, tzinfo=UTC), id="utc"),
            pytest.param(
                I255MyDT(2026, 2, 1, 12, 30, tzinfo=PLUS_TWO),
                datetime(2026, 2, 1, 12, 30, tzinfo=PLUS_TWO),
                id="plus_two",
            ),
        ],
    )
    def test_same_bytes_as_plain_datetime(self, subclass: datetime, plain: datetime) -> None:
        assert _encode_quietly({"d": subclass}) == _encode_quietly({"d": plain})
        assert _encode_quietly({"d": RecordId("t", [subclass])}) == _encode_quietly({"d": RecordId("t", [plain])})

    def test_never_sent_as_tag_12_with_a_string(self) -> None:
        """Tag 12 is SurrealDB's ``[seconds, nanoseconds]`` pair; an ISO string in it is a parse error."""
        raw = cbor2.loads(_encode_quietly({"d": I255MyDT(2026, 2, 1, tzinfo=UTC)}))
        assert raw == {"d": datetime(2026, 2, 1, tzinfo=UTC)}

    def test_fold_is_kept(self) -> None:
        folded = I255MyDT(2026, 11, 1, 1, 30, tzinfo=PLUS_TWO, fold=1)
        assert _encode_quietly(folded) == _encode_quietly(datetime(2026, 11, 1, 1, 30, tzinfo=PLUS_TWO, fold=1))

    def test_naive_subclass_warns(self) -> None:
        with pytest.warns(RuntimeWarning, match=WARNING):
            encode({"d": I255MyDT(2026, 2, 1)})
