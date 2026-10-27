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
- TAG_DATETIME (12): DateTime, received as an ISO 8601 string or a [seconds, nanoseconds]
  pair (a datetime is sent as the standard tag 0, which SurrealDB reads)
- TAG_STRING_DURATION (14): Duration as string
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
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
TAG_STRING_DURATION = 14

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
    """Represents a SurrealDB duration."""

    value: str

    def __str__(self) -> str:
        return self.value


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
    the ``date`` becomes that datetime, and cbor2 then encodes it the way it
    encodes every timezone-aware datetime (tag 0), which SurrealDB reads.

    A naive ``datetime`` (subclasses included) is left as it is but warned
    about with a ``RuntimeWarning`` (see :func:`_warn_naive_datetime`):
    :func:`encode` reads it as UTC, because SurrealDB only stores UTC instants.
    A naive ``datetime`` inside a ``RecordId``'s id is converted the same way
    but not warned about, since this walk does not enter record ids.
    """
    if data is None:
        return CBORTag(TAG_NONE, None)
    if isinstance(data, Decimal) and data.is_finite():
        return CBORTag(TAG_STRING_DECIMAL, str(data))
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

    cbor2 encodes a plain ``datetime`` itself (tag 0, an ISO 8601 string,
    which SurrealDB reads) and only sends a ``datetime`` *subclass* here, such
    as ``pandas.Timestamp`` or ``pendulum.DateTime``, wherever it meets one,
    record ids included. Such a value is rebuilt as a plain ``datetime`` (the
    same fields, ``tzinfo`` and ``fold``) and encoded again, so it travels
    exactly like one and a naive one is read as UTC. Digits below the
    microsecond that a subclass carries (``pandas.Timestamp.nanosecond``) are
    dropped, as they are for any Python ``datetime``. Tag 12 is never emitted
    with a string here: SurrealDB reads tag 12 only as a
    ``[seconds, nanoseconds]`` pair and rejects the request otherwise.
    """
    if isinstance(value, RecordId):
        # Encode RecordId as tagged array [table, id]
        encoder.encode(CBORTag(TAG_RECORDID, [value.table, value.id]))
    elif isinstance(value, Table):
        # Encode Table as tagged string
        encoder.encode(CBORTag(TAG_TABLE, value.name))
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
        # Encode UUID as tagged string
        encoder.encode(CBORTag(TAG_STRING_UUID, str(value)))
    elif isinstance(value, Duration):
        # Encode Duration as tagged string
        encoder.encode(CBORTag(TAG_STRING_DURATION, value.value))
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
    else:
        # Return raw tagged value for unknown tags
        return tag.value


def encode(data: Any) -> bytes:
    """
    Encode data to CBOR bytes using SurrealDB's custom tags.

    Pre-processes the data to convert Python ``None`` values to SurrealDB's
    ``NONE`` (CBORTag 6) instead of CBOR null (which maps to SurrealDB ``NULL``).

    A naive ``datetime`` is read as UTC, at any depth, and a ``RuntimeWarning``
    says so: SurrealDB only stores UTC instants, reads a date-only literal as
    midnight UTC and refuses a date-time with no offset. Attach a ``tzinfo``
    (``datetime.now(UTC)``) to send a different instant. A timezone-aware
    ``datetime`` is encoded unchanged.

    Args:
        data: Python object to encode

    Returns:
        CBOR-encoded bytes
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
