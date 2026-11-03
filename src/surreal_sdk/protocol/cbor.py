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
- TAG_STRING_UUID (9): UUID as string
- TAG_STRING_DECIMAL (10): Decimal as string
- TAG_DATETIME (12): DateTime (ISO 8601 string, or a [seconds, nanoseconds] pair)
- TAG_STRING_DURATION (13): Duration as a string ("1h30m")
- TAG_CUSTOM_DURATION (14): Duration as a compact [seconds, nanoseconds] array, the
  only form SurrealDB sends; it rejects a string under this tag
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Context, Decimal
from typing import Any
from uuid import UUID

import cbor2
from cbor2 import CBORTag

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

#: Shown for a naive ``datetime``. It names no value, so Python's default
#: warning filter reports it once per call site rather than once per value.
_NAIVE_DATETIME_WARNING = (
    "A naive datetime was sent to SurrealDB and read as UTC. "
    "Attach a tzinfo (for example datetime.now(UTC)) to send a different instant."
)


def _warn_naive_datetime() -> None:
    """Warn that a naive ``datetime`` is being sent as a UTC instant.

    SurrealDB only stores UTC instants, so a ``datetime`` with no ``tzinfo``
    is read as UTC. It is a ``RuntimeWarning`` attributed to the caller
    (``stacklevel=2``): the CBOR encoder and ``live()``'s literal renderer each
    report it once per process under the default filter.
    """
    warnings.warn(_NAIVE_DATETIME_WARNING, RuntimeWarning, stacklevel=2)


def needs_id_escaping(record_id: str) -> bool:
    """
    Check whether a record ID must be escaped in SurrealQL.

    IDs that start with a digit, or hold anything but letters, digits and
    underscores, are read as something else when written bare.

    Args:
        record_id: The record ID, without the table prefix

    Returns:
        True when the ID needs backticks
    """
    if not record_id:
        return False
    if record_id[0].isdigit():
        return True
    return not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", record_id)


def escape_record_id(record_id: str) -> str:
    r"""
    Escape a record ID for SurrealQL when it needs it.

    Uses SurrealDB's backtick-delimited form. Inside it SurrealDB reads ``\``
    as an escape character, so a backslash is written ``\\`` and a backtick
    ``\``` — the backslash first, otherwise the backslash added in front of a
    backtick would itself be doubled. :func:`unescape_record_id` reads the
    result back.

    Args:
        record_id: The record ID, without the table prefix

    Returns:
        The ID, backtick-wrapped when required

    Examples:
        escape_record_id("abc123")   # "abc123"
        escape_record_id("7abc")     # "`7abc`"
        escape_record_id("a`b")      # "`a\`b`"
        escape_record_id("dir\\")    # "`dir\\`"
    """
    if needs_id_escaping(record_id):
        escaped = record_id.replace("\\", "\\\\").replace("`", "\\`")
        return f"`{escaped}`"
    return record_id


#: One unit of a backtick-delimited record ID body: an escaped backslash or
#: backtick, a doubled backtick (the form older releases wrote), or any
#: other character.
_ESCAPED_ID_UNIT = re.compile(r"\\[\\`]|``|.", re.DOTALL)


def unescape_record_id(id_part: str) -> str:
    r"""
    Read back a record ID written by :func:`escape_record_id`.

    A backtick-delimited ID loses its delimiters, ``\\`` becomes ``\`` and
    ``\``` becomes a backtick. Those are the only two sequences the escaper
    produces, so every other backslash is kept as written. A doubled backtick
    is still read as one, so a string produced by older releases parses as it
    always did. An ID that is not backtick-delimited is returned unchanged.

    Args:
        id_part: The record ID part of a ``table:id`` reference

    Returns:
        The raw record ID
    """
    if len(id_part) < 2 or not (id_part.startswith("`") and id_part.endswith("`")):
        return id_part
    return _ESCAPED_ID_UNIT.sub(
        lambda m: m.group(0)[-1] if len(m.group(0)) == 2 else m.group(0),
        id_part[1:-1],
    )


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

    def to_surql(self) -> str:
        """
        Render the record as a SurrealQL thing reference.

        Unlike ``str()``, which gives the plain ``table:id`` form the ORM stores
        on a model, this escapes an id SurrealQL would otherwise read as
        something else. ``t:123`` is the *numeric* record; the string one is
        ``t:`123```, and they are two different rows.

        Returns:
            The escaped ``table:id`` reference
        """
        return f"{self.table}:{escape_record_id(str(self.id))}"

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

    ``value`` is either a duration string (``"1h30m"``), sent as CBOR tag 13,
    or the compact form SurrealDB itself sends, a sequence of seconds and
    nanoseconds (``[5400]``, ``[5400, 1]``, ``[]`` for zero), sent as tag 14.

    A compact value is stored as a new ``list``, whatever sequence it was built
    from: that is the form a duration read from the server holds, so
    ``Duration((5400, 1)) == Duration([5400, 1])``. A string is kept as is.
    """

    value: str | list[int] | tuple[int, ...]

    def __post_init__(self) -> None:
        """Store a compact value as a list, so equality does not depend on the container."""
        if not isinstance(self.value, str):
            self.value = list(self.value)

    def __str__(self) -> str:
        """Return a SurrealQL duration literal (``"1h30m"``, ``"5400s1ns"``, ``"0ns"``)."""
        if isinstance(self.value, str):
            return self.value
        seconds = self.value[0] if len(self.value) > 0 else 0
        nanoseconds = self.value[1] if len(self.value) > 1 else 0
        text = (f"{seconds}s" if seconds else "") + (f"{nanoseconds}ns" if nanoseconds else "")
        return text or "0ns"


#: The largest magnitude a SurrealDB decimal holds: its digits are a 96-bit integer.
_DECIMAL_MAX = Decimal(2**96 - 1)
#: The most places a SurrealDB decimal keeps after the point.
_DECIMAL_MAX_SCALE = 28
_DECIMAL_QUANTUM = Decimal(1).scaleb(-_DECIMAL_MAX_SCALE)
#: Enough precision for any in-range value at 28 places (29 + 28 digits).
_DECIMAL_CONTEXT = Context(prec=2 * _DECIMAL_MAX_SCALE + 4, rounding=ROUND_HALF_UP)


def format_decimal(value: Decimal) -> str:
    """
    Write a finite ``Decimal`` as the digits of the SurrealDB decimal it binds to.

    The text is positional (``"100"``, ``"0.00001"``), never scientific:
    SurrealDB 2.7.0 refuses ``"1E+2"`` as a bound decimal. A SurrealDB decimal
    holds at most 79228162514264337593543950335 in magnitude, and at most 28
    places after the point. Both limits are applied from the exponent, before
    any digit is written, so the length of the text never grows with the
    exponent:

    - a magnitude above the maximum raises ``ValueError``; SurrealDB rejects
      the whole request for such a value;
    - places beyond the 28th are rounded off, half away from zero, the way
      SurrealDB rounds them when it reads a longer value (checked on 2.7.0,
      for a bound value and a literal alike). ``Decimal(1) / 30`` is therefore
      sent as the value SurrealDB would store for it, and a tiny value such as
      ``Decimal("1E-40")`` as ``0.0000000000000000000000000000``.

    Args:
        value: The value to write

    Returns:
        The digits, e.g. ``"1.50"``, ``"-0.00001"``

    Raises:
        ValueError: The value is NaN or infinite, or its magnitude is above
            the largest SurrealDB decimal.
    """
    if not value.is_finite():
        raise ValueError(f"{value!r} has no SurrealDB decimal form")
    # adjusted() is the exponent of the leading digit: > 28 means at least 1E+29.
    if not value.is_zero() and (value.adjusted() > _DECIMAL_MAX_SCALE or value.copy_abs() > _DECIMAL_MAX):
        raise ValueError(f"Decimal value out of range for SurrealDB: {value!r} (largest magnitude {_DECIMAL_MAX})")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -_DECIMAL_MAX_SCALE:
        value = value.quantize(_DECIMAL_QUANTUM, context=_DECIMAL_CONTEXT)
    return format(value, "f")


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

    ``Decimal`` is tagged here too, as ``TAG_STRING_DECIMAL`` with the digits
    written by :func:`format_decimal`. cbor2 has a native ``Decimal`` encoder
    (tag 4, a decimal fraction), so ``_cbor_default_encoder`` is never called
    for one, and SurrealDB 2.x does not read tag 4: it rejects the whole
    request. It also refuses scientific notation in tag 10 (``"1E+2"``), so the
    digits are positional; an out-of-range value raises ``ValueError`` instead
    of being written out. NaN and infinities have no SurrealDB decimal form and
    keep cbor2's float encoding.

    A ``date`` (not a ``datetime``) has the same problem: cbor2 encodes it
    natively as tag 1004, which SurrealDB rejects. SurrealDB has no date-only
    type (the literal ``d'YYYY-MM-DD'`` is a datetime at midnight UTC), so the
    ``date`` becomes that datetime, and cbor2 then encodes it the way it
    encodes every timezone-aware datetime (tag 0), which SurrealDB reads.
    """
    if data is None:
        return CBORTag(TAG_NONE, None)
    if isinstance(data, Decimal) and data.is_finite():
        return CBORTag(TAG_STRING_DECIMAL, format_decimal(data))
    if isinstance(data, datetime):
        if data.tzinfo is None:
            _warn_naive_datetime()
        return data
    if isinstance(data, date):
        return datetime(data.year, data.month, data.day, tzinfo=UTC)
    if isinstance(data, dict):
        return {k: _preprocess_for_cbor(v) for k, v in data.items()}
    if isinstance(data, (list, tuple, set, frozenset)):
        converted = [_preprocess_for_cbor(item) for item in data]
        return type(data)(converted)
    return data


def _cbor_default_encoder(encoder: Any, value: Any) -> None:
    """
    Custom CBOR encoder for SurrealDB types.

    Handles encoding of Python types to SurrealDB CBOR format.

    Note: String record IDs (like "table:id") are NOT automatically converted
    to RecordId objects. The caller must use RecordId objects explicitly when
    needed (e.g., for record references in relation operations).

    There is no ``datetime`` branch: cbor2 encodes every ``datetime``,
    subclasses included, itself (tag 0, an ISO 8601 string), and :func:`encode`
    gives it ``timezone=UTC`` for a naive one, so this hook never sees one.
    """
    if isinstance(value, RecordId):
        # Encode RecordId as tagged array [table, id]
        encoder.encode(CBORTag(TAG_RECORDID, [value.table, value.id]))
    elif isinstance(value, Table):
        # Encode Table as tagged string
        encoder.encode(CBORTag(TAG_TABLE, value.name))
    elif isinstance(value, UUID):
        # Encode UUID as tagged string
        encoder.encode(CBORTag(TAG_STRING_UUID, str(value)))
    elif isinstance(value, Duration):
        # Tag 14 carries only the compact array: a string there is a parse
        # error for the whole request (HTTP 400, no reply over WebSocket).
        if isinstance(value.value, str):
            encoder.encode(CBORTag(TAG_STRING_DURATION, value.value))
        else:
            encoder.encode(CBORTag(TAG_CUSTOM_DURATION, list(value.value)))
    else:
        # Fall back to raising TypeError for unsupported types
        raise TypeError(f"Cannot CBOR encode {type(value)}")


def _cbor_tag_decoder(decoder: Any, tag: Any) -> Any:
    """
    Custom CBOR tag decoder for SurrealDB types.

    Handles decoding of SurrealDB CBOR tags to Python types.
    """
    if tag.tag == TAG_NONE:
        return None
    elif tag.tag == TAG_TABLE:
        return Table(name=tag.value)
    elif tag.tag == TAG_RECORDID:
        # RecordId is encoded as [table, id]
        if isinstance(tag.value, list) and len(tag.value) == 2:
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
        # [seconds, nanoseconds] pair, and 2.x uses the compact form for every
        # datetime over CBOR RPC. Decoding it here is what makes datetimes
        # outside a typed model field (raw queries, values nested in objects)
        # come back as datetimes rather than as a list of two ints. The seconds
        # are floored: 1969-12-31T23:59:59.5Z arrives as [-1, 500000000].
        #
        # Both branches fall back to the raw value rather than raising: cbor2
        # re-raises an error from this hook as CBORDecodeError for the whole
        # response, and SurrealDB's datetime range is wider than Python's
        # years 1-9999, so one unrepresentable value would lose every other
        # value in the same response.
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
    elif tag.tag in (TAG_STRING_DURATION, TAG_CUSTOM_DURATION):
        # 13 holds a string, 14 the compact [seconds, nanoseconds] array
        # (a list under cbor2 5.x), which is what the server sends.
        return Duration(value=tag.value)
    else:
        # Return raw tagged value for unknown tags
        return tag.value


def encode(data: Any) -> bytes:
    """
    Encode data to CBOR bytes using SurrealDB's custom tags.

    Pre-processes the data to convert Python ``None`` values to SurrealDB's
    ``NONE`` (CBORTag 6) instead of CBOR null (which maps to SurrealDB ``NULL``).

    Args:
        data: Python object to encode

    Returns:
        CBOR-encoded bytes

    A naive ``datetime`` is read as UTC, at any depth, and a ``RuntimeWarning``
    says so: SurrealDB only stores UTC instants. Attach a ``tzinfo``
    (``datetime.now(UTC)``) to send a different instant. A timezone-aware
    ``datetime`` is encoded unchanged.

    Raises:
        ValueError: A ``Decimal`` is above the largest SurrealDB decimal in
            magnitude (see :func:`format_decimal`).
    """
    processed = _preprocess_for_cbor(data)
    result: bytes = cbor2.dumps(processed, default=_cbor_default_encoder, timezone=UTC)
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
