"""
CBOR Encoding/Decoding for SurrealDB Protocol.

This module provides CBOR serialization support using SurrealDB's custom tags.
CBOR (Concise Binary Object Representation) is the default protocol for
SurrealDB as it properly handles binary data and avoids string interpretation
issues that occur with JSON (e.g., 'data:xxx' being interpreted as record links).

Custom CBOR Tags used by SurrealDB:
- TAG_NONE (6): None/null value
- TAG_TABLE (7): Table name
- TAG_RECORDID (8): Record ID (table:id)
- TAG_STRING_UUID (9): UUID as string, decoded only. A ``UUID`` (a subclass
  included) is sent as the standard tag 37 (cbor2's native encoding);
  SurrealDB 4.0 refuses tag 9.
- TAG_STRING_DECIMAL (10): Decimal as string
- TAG_DATETIME (12): DateTime as a compact [seconds, nanoseconds] pair, the form
  every ``datetime`` and ``date`` is sent in; an ISO 8601 string under this tag
  is still decoded
- TAG_STRING_DURATION (13): Duration as a string ("1h30m"), decoded only
- TAG_CUSTOM_DURATION (14): Duration as a compact [seconds, nanoseconds] array, the
  form every ``Duration`` is sent in and the only one SurrealDB sends; it rejects
  a string under this tag
- TAG_FILE (55): File pointer as a [bucket, key] array (``FileRef``), both ways

Tags 0 (ISO datetime string), 9 and 13 are never sent: SurrealDB 3.x reads them,
4.0 refuses them, and every server since 1.5 reads tags 12 and 14.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import cbor2
from cbor2 import CBORTag

from ..files import FileRef

# CBOR is always available (required dependency)
CBOR_AVAILABLE = True


# SurrealDB Custom CBOR Tags
TAG_NONE = 6
TAG_TABLE = 7
TAG_RECORDID = 8
TAG_STRING_UUID = 9
TAG_STRING_DECIMAL = 10
TAG_DATETIME = 12
TAG_STRING_DURATION = 13
TAG_CUSTOM_DURATION = 14
TAG_FILE = 55

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NANOS_PER_SECOND = 1_000_000_000
#: SurrealDB stores a duration's seconds as an unsigned 64-bit integer.
_MAX_DURATION_SECONDS = 2**64 - 1
#: The integers CBOR carries without a bignum tag (major types 0 and 1).
_CBOR_INT_MIN = -(2**64)
_CBOR_INT_MAX = 2**64 - 1

#: Nanoseconds per SurrealDB duration unit (``y`` is 365 days, as on the server).
_DURATION_UNITS = {
    "ns": 1,
    "us": 1_000,
    "\u00b5s": 1_000,
    "ms": 1_000_000,
    "s": _NANOS_PER_SECOND,
    "m": 60 * _NANOS_PER_SECOND,
    "h": 3_600 * _NANOS_PER_SECOND,
    "d": 86_400 * _NANOS_PER_SECOND,
    "w": 604_800 * _NANOS_PER_SECOND,
    "y": 31_536_000 * _NANOS_PER_SECOND,
}
#: One ``<digits><unit>`` part. Two-letter units come first so ``ms`` is not ``m`` + ``s``.
_DURATION_PART = re.compile(r"([0-9]+)(ns|us|\u00b5s|ms|s|m|h|d|w|y)")
_DURATION_TEXT = re.compile(r"(?:[0-9]+(?:ns|us|\u00b5s|ms|s|m|h|d|w|y))+")

#: Shown for a naive ``datetime``. It names no value, so Python's default
#: warning filter reports it once per call site rather than once per value.
_NAIVE_DATETIME_WARNING = (
    "A naive datetime was sent to SurrealDB and read as UTC. "
    "Attach a tzinfo (for example datetime.now(UTC)) to send a different instant."
)


def _warn_naive_datetime() -> None:
    """Warn that a naive ``datetime`` is being sent as a UTC instant.

    SurrealDB only stores UTC instants, so a ``datetime`` with no ``tzinfo``
    is read as UTC; the warning mirrors Django's under ``USE_TZ``. It is a
    ``RuntimeWarning`` attributed to the caller (``stacklevel=2``): the CBOR
    encoder and the ORM's inline-literal path (``raw_query(inline_dicts=True)``)
    each report it once per process under the default filter.
    """
    warnings.warn(_NAIVE_DATETIME_WARNING, RuntimeWarning, stacklevel=2)


@dataclass
class RecordId:
    """
    Represents a SurrealDB Record ID (table:id format).

    Attributes:
        table: The table name
        id: The record identifier (can be string, int, or complex object)
    """

    table: str
    id: Any

    def __str__(self) -> str:
        """Return the full record ID string."""
        return f"{self.table}:{self.id}"

    @classmethod
    def parse(cls, value: str) -> RecordId:
        """Parse a record ID string into a RecordId object."""
        if ":" in value:
            table, id_part = value.split(":", 1)
            return cls(table=table, id=id_part)
        raise ValueError(f"Invalid record ID format: {value}")


@dataclass
class Table:
    """Represents a SurrealDB table reference."""

    name: str

    def __str__(self) -> str:
        return self.name


@dataclass
class Duration:
    """
    Represents a SurrealDB duration.

    ``value`` is either a SurrealQL duration text (``"1h30m"``) or the compact
    form SurrealDB itself sends, a tuple of seconds and nanoseconds
    (``(5400,)``, ``(5400, 1)``, ``()`` for zero). Both are sent as CBOR tag 14
    ``[seconds, nanoseconds]``: a text value is parsed when it is encoded, and
    an invalid one raises ``ValueError`` then, before any request. A value
    the server returns is always the tuple form, so ``Duration("1h30m")``
    comes back as ``Duration((5400,))``.
    """

    value: str | tuple[int, ...]

    def __str__(self) -> str:
        """Return a SurrealQL duration literal (``"1h30m"``, ``"5400s1ns"``, ``"0ns"``)."""
        if isinstance(self.value, str):
            return self.value
        seconds = self.value[0] if len(self.value) > 0 else 0
        nanoseconds = self.value[1] if len(self.value) > 1 else 0
        text = (f"{seconds}s" if seconds else "") + (f"{nanoseconds}ns" if nanoseconds else "")
        return text or "0ns"


def _parse_duration_text(text: str) -> tuple[int, int]:
    """Read a SurrealQL duration text (``"1h30m"``) as ``(seconds, nanoseconds)``.

    Accepts what SurrealDB's duration literal accepts: one or more
    ``<digits><unit>`` parts with no separator, in any order, repeated units
    added up (``"30m1h"`` and ``"1h1h"`` are valid), units ``ns``, ``us``/``µs``,
    ``ms``, ``s``, ``m``, ``h``, ``d``, ``w`` and ``y`` (365 days). Signs,
    fractions, spaces, upper case and a seconds total above the unsigned
    64-bit range are refused by the server, and raise ``ValueError`` here.
    """
    if not _DURATION_TEXT.fullmatch(text):
        raise ValueError(f"Invalid SurrealDB duration text: {text!r}")
    total = sum(int(amount) * _DURATION_UNITS[unit] for amount, unit in _DURATION_PART.findall(text))
    seconds, nanoseconds = divmod(total, _NANOS_PER_SECOND)
    if seconds > _MAX_DURATION_SECONDS:
        raise ValueError(f"SurrealDB duration out of range: {text!r}")
    return seconds, nanoseconds


def _encode_datetime(encoder: Any, value: datetime) -> None:
    """Write a ``datetime`` as tag 12 ``[seconds, nanoseconds]`` since the Unix epoch.

    A naive value (no ``tzinfo``, or one whose ``utcoffset()`` is ``None``) is
    read as UTC. ``timedelta`` normalisation keeps the nanoseconds in
    ``[0, 10**9)`` and floors the seconds, so an instant before 1970 has
    negative seconds and positive nanoseconds (``-0.5 s`` is ``[-1, 500000000]``),
    the form SurrealDB reads and the decoder below reverses.
    """
    if value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    delta = value - _EPOCH
    seconds = delta.days * 86_400 + delta.seconds
    encoder.encode(CBORTag(TAG_DATETIME, [seconds, delta.microseconds * 1_000]))


def _preprocess_for_cbor(data: Any) -> Any:
    """
    Pre-process data before CBOR encoding to handle None → NONE correctly.

    cbor2 natively encodes ``None`` as CBOR null, which SurrealDB interprets
    as ``NULL``.  SurrealDB distinguishes ``NULL`` (explicit null) from
    ``NONE`` (absent/unset), and SCHEMAFULL tables with ``option<T>`` fields
    reject ``NULL``.

    This function recursively walks common container types (dicts, lists,
    tuples, sets), replacing Python ``None`` with ``CBORTag(TAG_NONE, None)``
    so that SurrealDB receives the correct NONE value instead of NULL.

    ``Decimal`` is tagged here too, as ``TAG_STRING_DECIMAL`` with the exact
    digits. cbor2 has a native ``Decimal`` encoder (tag 4, a decimal
    fraction), so ``_cbor_default_encoder`` is never called for one, and
    SurrealDB does not read tag 4 — it rejects the whole request as a parse
    error. NaN and infinities have no SurrealDB decimal form and keep cbor2's
    float encoding.

    A ``date`` (not a ``datetime``) has the same problem: cbor2 encodes it
    natively as tag 1004, which SurrealDB rejects. SurrealDB has no date-only
    type — the literal ``d'YYYY-MM-DD'`` is a datetime at midnight UTC — so
    the ``date`` becomes that datetime, which :func:`encode` then sends as
    tag 12 like any other.

    A naive ``datetime`` (subclasses included) is left as it is but warned
    about with a ``RuntimeWarning`` (see :func:`_warn_naive_datetime`):
    :func:`encode` reads it as UTC, because SurrealDB only stores UTC instants.

    A ``RecordId``'s id goes through its own walk (:func:`_convert_record_id`),
    which checks integers, turns a ``date`` into the UTC-midnight datetime and
    warns once about a naive ``datetime``, at any depth (lists, tuples, sets,
    dict values, nested record ids; dict keys are checked). Nothing else
    inside a record id is converted (``None`` and ``Decimal`` keep cbor2's
    encoding there), and the caller's ``RecordId`` is never modified: a
    converted copy is returned only when something changed.

    An integer outside the 64-bit range CBOR carries natively (below
    ``-2**64`` or above ``2**64 - 1``) raises ``ValueError``, wherever it is
    met: a value, a dict key (tuple keys included) or inside a record id.
    cbor2 would send it as a bignum tag, which SurrealDB rejects as a parse
    error (HTTP 400, no reply over WebSocket). An integer inside that range
    but outside the signed 64-bit range is sent unchanged, as before:
    SurrealDB 3.3 accepts it and wraps it into a signed 64-bit integer, 4.0
    refuses it. Dict keys are only checked, never converted.
    """
    if data is None:
        return CBORTag(TAG_NONE, None)
    if isinstance(data, int) and not isinstance(data, bool):
        return _check_cbor_int(data)
    if isinstance(data, Decimal) and data.is_finite():
        return CBORTag(TAG_STRING_DECIMAL, str(data))
    if isinstance(data, datetime):
        if data.tzinfo is None:
            _warn_naive_datetime()
        return data
    if isinstance(data, date):
        return datetime(data.year, data.month, data.day, tzinfo=UTC)
    if isinstance(data, RecordId):
        return _convert_record_id(data)
    if isinstance(data, dict):
        return {_check_key(k): _preprocess_for_cbor(v) for k, v in data.items()}
    if isinstance(data, (list, tuple, set, frozenset)):
        converted = [_preprocess_for_cbor(item) for item in data]
        # Tuple subclasses such as NamedTuple take positional fields rather than
        # an iterable. They have the same CBOR array representation as tuples.
        return tuple(converted) if isinstance(data, tuple) else type(data)(converted)
    return data


def _check_cbor_int(value: int) -> int:
    """Return ``value`` if CBOR carries it without a bignum tag, else raise ``ValueError``."""
    if not _CBOR_INT_MIN <= value <= _CBOR_INT_MAX:
        raise ValueError(
            f"Integer {value} is outside the 64-bit range: CBOR would send it as a bignum, which SurrealDB rejects"
        )
    return value


def _check_key(key: Any) -> Any:
    """Range-check the integers of a dict key and return the key unchanged.

    A key is hashable, so the only containers it can hold are tuples and
    frozensets, which cannot hold themselves: no cycle guard is needed.
    """
    if isinstance(key, int) and not isinstance(key, bool):
        _check_cbor_int(key)
    elif isinstance(key, (tuple, frozenset)):
        for item in key:
            _check_key(item)
    return key


class _RecordIdWalk:
    """One pass over a record id's value, for :func:`_convert_record_id`.

    ``done`` maps each container already converted (by ``id()``) to its
    result, so a container met twice is converted the same way both times.
    ``active`` holds the containers being walked: meeting one again is a
    cycle, and the original is returned so cbor2 still refuses it with
    ``CBOREncodeValueError``, as it did before this walk existed. The ids
    are those of the caller's objects, which stay alive during the walk.
    """

    def __init__(self) -> None:
        self.done: dict[int, Any] = {}
        self.active: set[int] = set()
        self.warned = False

    def visit(self, value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool):
            return _check_cbor_int(value)
        if isinstance(value, datetime):
            if value.tzinfo is None and not self.warned:
                self.warned = True
                _warn_naive_datetime()
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, tzinfo=UTC)
        if not isinstance(value, (RecordId, dict, list, tuple, set, frozenset)):
            return value
        key = id(value)
        if key in self.done:
            return self.done[key]
        if key in self.active:
            return value
        self.active.add(key)
        try:
            result = self._visit_container(value)
        finally:
            self.active.discard(key)
        self.done[key] = result
        return result

    def _visit_container(
        self, value: RecordId | dict[Any, Any] | list[Any] | tuple[Any, ...] | set[Any] | frozenset[Any]
    ) -> Any:
        if isinstance(value, RecordId):
            inner = self.visit(value.id)
            return value if inner is value.id else RecordId(table=value.table, id=inner)
        if isinstance(value, dict):
            items = [(_check_key(k), self.visit(v)) for k, v in value.items()]
            if all(new is old for (_, new), old in zip(items, value.values(), strict=True)):
                return value
            return dict(items)
        converted = [self.visit(item) for item in value]
        if all(new is old for new, old in zip(converted, value, strict=True)):
            return value
        return type(value)(converted)


def _convert_record_id(record_id: RecordId) -> RecordId:
    """Check and convert a record id's value before it is encoded.

    Integers outside the 64-bit range raise ``ValueError`` (the message
    :func:`_preprocess_for_cbor` gives for a plain value), a ``date`` (not a
    ``datetime``) becomes the UTC-midnight ``datetime`` that :func:`encode`
    sends as tag 12, and the first naive ``datetime`` warns once (it is read
    as UTC). The walk enters lists, tuples, sets, dict values and nested
    ``RecordId`` ids; dict keys are range-checked (:func:`_check_key`) but not
    converted, as anywhere else. Nothing else is converted.

    The caller's ``RecordId`` and its containers are never modified: the
    record id is returned as is when nothing changed, and otherwise a new
    ``RecordId`` holding new containers on the changed paths.
    """
    result: RecordId = _RecordIdWalk().visit(record_id)
    return result


def _cbor_default_encoder(encoder: Any, value: Any) -> None:
    """
    Custom CBOR encoder for SurrealDB types.

    Handles encoding of Python types to SurrealDB CBOR format.

    Note: String record IDs (like "table:id") are NOT automatically converted
    to RecordId objects. The caller must use RecordId objects explicitly when
    needed (e.g., for record references in relation operations).

    :func:`encode` gives cbor2 an encoder for a plain ``datetime``
    (:func:`_encode_datetime`, tag 12), and cbor2 looks encoders up by exact
    type, so only a ``datetime`` *subclass* reaches this hook, such as
    ``pandas.Timestamp`` or ``pendulum.DateTime``, wherever it is met, record
    ids included. Such a value is rebuilt as a plain ``datetime`` (the same
    fields, ``tzinfo`` and ``fold``) and encoded again, so it travels exactly
    like one and a naive one is read as UTC. Digits below the microsecond that
    a subclass carries (``pandas.Timestamp.nanosecond``) are dropped, as they
    are for any Python ``datetime``.

    A ``Duration`` is always sent as tag 14 ``[seconds, nanoseconds]``, in the
    compact form the server itself sends (trailing zeros dropped). A text
    value is parsed first (:func:`_parse_duration_text`); an invalid one raises
    ``ValueError`` before anything is sent.

    A plain ``UUID`` never reaches this hook: cbor2 encodes it natively as
    tag 37, which SurrealDB reads. A ``UUID`` *subclass* does (exact-type
    lookup again), such as ``asyncpg``'s ``pgproto.UUID``; it is rebuilt as a
    plain ``UUID`` from its 128-bit value and encoded again, so it goes out as
    tag 37 too, never as tag 9.
    """
    if isinstance(value, RecordId):
        # Encode RecordId as tagged array [table, id]
        encoder.encode(CBORTag(TAG_RECORDID, [value.table, value.id]))
    elif isinstance(value, Table):
        # Encode Table as tagged string
        encoder.encode(CBORTag(TAG_TABLE, value.name))
    elif isinstance(value, FileRef):
        # A dataclass is not native to cbor2, so it reaches this hook (a
        # NamedTuple would have gone out as a plain array).
        encoder.encode(CBORTag(TAG_FILE, [value.bucket, value.key]))
    elif isinstance(value, datetime):
        encoder.encode(
            datetime(
                value.year,
                value.month,
                value.day,
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
                tzinfo=value.tzinfo,
                fold=value.fold,
            )
        )
    elif isinstance(value, UUID):
        encoder.encode(UUID(int=value.int))
    elif isinstance(value, Duration):
        # Tag 14 carries only the compact array: a string there is a parse
        # error for the whole request (HTTP 400, no reply over WebSocket).
        # Tag 13 (the string form) is refused by SurrealDB 4.0.
        if isinstance(value.value, str):
            seconds, nanoseconds = _parse_duration_text(value.value)
            if nanoseconds:
                payload = [seconds, nanoseconds]
            elif seconds:
                payload = [seconds]
            else:
                payload = []
            encoder.encode(CBORTag(TAG_CUSTOM_DURATION, payload))
        else:
            encoder.encode(CBORTag(TAG_CUSTOM_DURATION, list(value.value)))
    else:
        # Fall back to raising TypeError for unsupported types
        raise TypeError(f"Cannot CBOR encode {type(value)}")


def _cbor_tag_decoder(tag: Any, immutable: bool) -> Any:
    """
    Custom CBOR tag decoder for SurrealDB types.

    Handles decoding of SurrealDB CBOR tags to Python types.

    Signature follows the cbor2 6.x ``tag_hook`` contract:
    ``(CBORTag, immutable)``. The ``immutable`` flag is unused here because all
    SurrealDB custom tags decode to mutable/standalone Python objects.
    """
    if tag.tag == TAG_NONE:
        return None
    elif tag.tag == TAG_TABLE:
        return Table(name=tag.value)
    elif tag.tag == TAG_RECORDID:
        # RecordId is encoded as [table, id]
        # cbor2 6.x decodes nested CBOR arrays as tuples, 5.x used lists.
        if isinstance(tag.value, list | tuple) and len(tag.value) == 2:
            return RecordId(table=tag.value[0], id=tag.value[1])
        # Some versions may encode as string
        elif isinstance(tag.value, str):
            return RecordId.parse(tag.value)
        return tag.value
    elif tag.tag == TAG_STRING_UUID:
        return UUID(tag.value)
    elif tag.tag == TAG_STRING_DECIMAL:
        return Decimal(tag.value)
    elif tag.tag == TAG_DATETIME:
        # SurrealDB sends tag 12 either as an ISO 8601 string or as a compact
        # [seconds, nanoseconds] pair, and 3.x uses the compact form for every
        # datetime. Decoding it here is what makes datetimes outside a typed
        # model field — raw queries, values nested in objects — come back as
        # datetimes rather than as a pair of ints.
        #
        # Both branches fall back to the raw value rather than raising: SurrealDB's
        # datetime range is far wider than Python's year 1-9999, so a single
        # out-of-range value would otherwise abort the decode of the *whole*
        # response with CBORDecodeError instead of affecting just that field.
        if isinstance(tag.value, str):
            try:
                return datetime.fromisoformat(tag.value.replace("Z", "+00:00"))
            except ValueError:
                return tag.value
        if isinstance(tag.value, (list, tuple)) and len(tag.value) == 2:
            seconds, nanoseconds = tag.value
            if isinstance(seconds, int) and isinstance(nanoseconds, int):
                try:
                    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanoseconds // 1000)
                except (ValueError, OverflowError, OSError):
                    return tag.value
        return tag.value
    elif tag.tag == TAG_STRING_DURATION:
        return Duration(value=tag.value)
    elif tag.tag == TAG_CUSTOM_DURATION:
        if isinstance(tag.value, (list, tuple)):
            return Duration(value=tuple(tag.value))
        return Duration(value=tag.value)
    elif tag.tag == TAG_FILE:
        # A file pointer is [bucket, key]. It is built without validation so a
        # key written by other means (``/../x`` on a memory bucket) still
        # decodes; anything else is returned raw rather than raising, which
        # would fail the decode of the whole response.
        value = tag.value
        if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(part, str) for part in value):
            return FileRef._from_wire(value[0], value[1])
        return value
    else:
        # Return raw tagged value for unknown tags
        return tag.value


def encode(data: Any) -> bytes:
    """
    Encode data to CBOR bytes using SurrealDB's custom tags.

    Pre-processes the data to convert Python ``None`` values to SurrealDB's
    ``NONE`` (CBORTag 6) instead of CBOR null (which maps to SurrealDB ``NULL``).

    Every ``datetime`` and ``date`` is sent as tag 12 and every ``Duration``
    as tag 14, both ``[seconds, nanoseconds]``, at any depth, record ids
    included. A naive ``datetime`` is read as UTC and a ``RuntimeWarning``
    says so: SurrealDB only stores UTC instants, reads a date-only literal as
    midnight UTC and refuses a date-time with no offset. Attach a ``tzinfo``
    (``datetime.now(UTC)``) to send a different instant.

    Raises:
        ValueError: for an invalid duration text or an integer outside the
            64-bit range (a value, a dict key or inside a record id), before
            anything is sent.

    Args:
        data: Python object to encode

    Returns:
        CBOR-encoded bytes
    """
    processed = _preprocess_for_cbor(data)
    result: bytes = cbor2.dumps(processed, default=_cbor_default_encoder, encoders={datetime: _encode_datetime})
    return result


def decode(data: bytes) -> Any:
    """
    Decode CBOR bytes to Python objects using SurrealDB's custom tags.

    Args:
        data: CBOR-encoded bytes

    Returns:
        Decoded Python object
    """
    return cbor2.loads(data, tag_hook=_cbor_tag_decoder)


def is_available() -> bool:
    """Check if CBOR support is available. Always returns True (cbor2 is required)."""
    return True
