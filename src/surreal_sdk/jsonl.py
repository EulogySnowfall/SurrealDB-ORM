"""
JSON Lines codec for decoded SurrealDB values.

The only place where a value the SDK decodes from a SurrealDB reply (CBOR
protocol) becomes JSON text, and back. A record is one line: a JSON object
whose SurrealDB-specific values are **tagged**, each tag a JSON object with
exactly one key:

========================  =============================================
``{"$record": [t, id]}``  :class:`~surreal_sdk.protocol.cbor.RecordId`
``{"$table": "t"}``       :class:`~surreal_sdk.protocol.cbor.Table`
``{"$datetime": "…Z"}``   aware UTC ``datetime`` (6 fraction digits)
``{"$decimal": "1.10"}``  ``Decimal``
``{"$uuid": "…"}``        ``UUID``
``{"$duration": …}``      :class:`~surreal_sdk.protocol.cbor.Duration`
                          (a string, or the compact ``[s, ns]`` array)
``{"$bytes": "…"}``       ``bytes`` (standard base64)
``{"$float": "NaN"}``     ``nan``, ``Infinity``, ``-Infinity``
``{"$object": {…}}``      a one-key object whose key starts with ``$``
========================  =============================================

A value the codec cannot carry losslessly is refused at export with
:class:`~surreal_sdk.exceptions.DataFormatError` rather than written as
something else: a ``tuple`` outside a record id (how the SDK decodes a
geometry, a range or an out-of-range datetime), a ``set``, a non-finite
``Decimal``, any other object. A malformed line is refused at import the same
way, so a typo in a tag never silently becomes data.

``typed=False`` writes plain JSON for external tools (record ids as
``table:id`` text, datetimes as ISO strings, …); such a file reads back as
plain JSON, strings staying strings.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final
from uuid import UUID

from .exceptions import DataFormatError
from .protocol.cbor import Duration, RecordId, Table, _warn_naive_datetime
from .utils import format_record_id

__all__ = [
    "JSONL_TAGS",
    "ImportReport",
    "decode_value",
    "dumps_record",
    "encode_value",
    "loads_record",
]

#: Every tag the codec writes and reads.
JSONL_TAGS: Final[frozenset[str]] = frozenset(
    {"$record", "$table", "$datetime", "$decimal", "$uuid", "$duration", "$bytes", "$float", "$object"}
)

#: SurrealDB's duration literal (M48): one or more ``<digits><unit>`` parts.
#: An invalid duration fails the whole request it is sent in (HTTP 400, or no
#: reply over WebSocket), so it is checked on the line that holds it.
_DURATION_RE: Final = re.compile(r"(?:[0-9]+(?:ns|us|µs|ms|s|m|h|d|w|y))+")

_FLOAT_TAGS: Final[dict[str, float]] = {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}

_GEOMETRY_REASON: Final = (
    "a tuple is how the SDK decodes a geometry, a range or an out-of-range datetime, "
    "so it would not round-trip; use dump_database() for a lossless copy"
)

#: Characters ``str.splitlines()`` treats as line breaks and ``json.dumps``
#: leaves alone under ``ensure_ascii=False``; escaped so a line stays one line
#: for every reader.
_LINE_BREAKS: Final = str.maketrans({"\x85": "\\u0085", "\u2028": "\\u2028", "\u2029": "\\u2029"})


@dataclass(frozen=True)
class ImportReport:
    """What a JSON Lines import wrote.

    Attributes:
        table: The target table.
        lines: Non-blank lines read and decoded.
        rows: Rows sent in accepted batches (with ``on_conflict="ignore"``,
            skipped duplicates included).
        batches: Accepted ``INSERT`` statements.
    """

    table: str
    lines: int
    rows: int
    batches: int


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def encode_value(value: Any, *, typed: bool = True, _path: str = "", _record: Any = None) -> Any:
    """
    Convert a decoded SurrealDB value to a JSON-compatible value.

    Args:
        value: A value as the SDK decodes it from a CBOR reply.
        typed: ``True`` (default) writes the lossless tagged form;
            ``False`` writes plain JSON for external tools.
        _path: Dotted path of ``value`` in its record (error messages).
        _record: The record's ``id`` (error messages).

    Returns:
        A value ``json.dumps(..., allow_nan=False)`` accepts.

    Raises:
        DataFormatError: If the value cannot be carried losslessly (E2).
    """
    return _encode(value, typed, _path, _record, in_id=False)


def dumps_record(record: Mapping[str, Any], *, typed: bool = True) -> str:
    """
    Write one record as one JSON line (no trailing newline).

    The text is compact, keeps non-ASCII characters as they are, and never
    contains a line break: those inside values are escaped.

    Args:
        record: The record, as the SDK decodes it.
        typed: See :func:`encode_value`.

    Returns:
        str: The JSON text.

    Raises:
        DataFormatError: If a value cannot be carried losslessly (E2).
    """
    encoded = _encode(record, typed, "", record.get("id"), in_id=False)
    text = json.dumps(encoded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.translate(_LINE_BREAKS)


def _encode(value: Any, typed: bool, path: str, record: Any, *, in_id: bool) -> Any:
    """Recursive worker of :func:`encode_value`; ``in_id`` accepts tuples (record id keys)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isfinite(value):
            return float(value)
        name = "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
        return {"$float": name} if typed else name
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise _unsupported(value, path, record)
        return {"$decimal": str(value)} if typed else str(value)
    if isinstance(value, datetime):
        text = _iso_utc(value, path, record)
        return {"$datetime": text} if typed else text
    if isinstance(value, date):
        text = _iso_utc(datetime(value.year, value.month, value.day, tzinfo=UTC), path, record)
        return {"$datetime": text} if typed else text
    if isinstance(value, UUID):
        return {"$uuid": str(value)} if typed else str(value)
    if isinstance(value, Duration):
        return _encode_duration(value, typed, path, record)
    if isinstance(value, (bytes, bytearray)):
        text = base64.b64encode(bytes(value)).decode("ascii")
        return {"$bytes": text} if typed else text
    if isinstance(value, RecordId):
        return _encode_record_id(value, typed, path, record)
    if isinstance(value, Table):
        if not isinstance(value.name, str):
            raise _unsupported(value, path, record)
        return {"$table": value.name} if typed else value.name
    if isinstance(value, Mapping):
        encoded: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise _unsupported(key, path, record)
            encoded[key] = _encode(item, typed, f"{path}.{key}" if path else key, record, in_id=in_id)
        if typed and len(encoded) == 1 and next(iter(encoded)).startswith("$"):
            return {"$object": encoded}
        return encoded
    if isinstance(value, list) or (in_id and isinstance(value, tuple)):
        return [_encode(item, typed, f"{path}[{index}]", record, in_id=in_id) for index, item in enumerate(value)]
    if isinstance(value, tuple):
        raise DataFormatError(
            f"Cannot write {_where(path, record)} as JSON Lines: {_GEOMETRY_REASON}",
            path=path or None,
        )
    raise _unsupported(value, path, record)


def _encode_duration(value: Duration, typed: bool, path: str, record: Any) -> Any:
    """Encode a ``Duration`` whose ``value`` is a string or a compact tuple of 0-2 non-negative ints."""
    raw = value.value
    if isinstance(raw, str):
        return {"$duration": raw} if typed else raw
    if (
        isinstance(raw, tuple)
        and len(raw) <= 2
        and all(isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in raw)
    ):
        # Checked above, so str() is total here.
        return {"$duration": [int(part) for part in raw]} if typed else str(value)
    raise _unsupported(value, path, record)


def _encode_record_id(value: RecordId, typed: bool, path: str, record: Any) -> Any:
    """Encode a ``RecordId``: tagged ``[table, key]`` or ``table:key`` text."""
    if not isinstance(value.table, str):
        raise _unsupported(value, path, record)
    key = value.id
    if typed:
        return {"$record": [value.table, _encode(key, True, path, record, in_id=True)]}
    if isinstance(key, (str, UUID)) or (isinstance(key, int) and not isinstance(key, bool)):
        return format_record_id(value)
    return f"{value.table}:{_compact_json(_encode(key, False, path, record, in_id=True))}"


def _relaxed_key_json(key: Any) -> str:
    """Compact JSON of an array or object record-id key (``format_record_id()``)."""
    return _compact_json(_encode(key, False, "", None, in_id=True))


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _iso_utc(value: datetime, path: str, record: Any) -> str:
    """ISO 8601 text in UTC with ``Z`` and 6 fraction digits; naive read as UTC (CBOR rule)."""
    if value.tzinfo is None:
        _warn_naive_datetime()
    # Rebuilt as a plain datetime: a subclass (pandas.Timestamp) formats differently.
    plain = datetime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        tzinfo=value.tzinfo or UTC,
        fold=value.fold,
    )
    try:
        utc = plain.astimezone(UTC)
    except OverflowError:
        raise _unsupported(value, path, record) from None
    return utc.replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


def _where(path: str, record: Any) -> str:
    """``{path} of {record}`` for an E2 message."""
    owner = "a record without id" if record is None else str(record)
    return f"{path or 'value'} of {owner}"


def _unsupported(value: Any, path: str, record: Any) -> DataFormatError:
    return DataFormatError(
        f"Cannot write {_where(path, record)} as JSON Lines: unsupported value of type {type(value).__name__}",
        path=path or None,
    )


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def decode_value(value: Any, *, _line: int | None = None) -> Any:
    """
    Convert a JSON value (as ``json.loads`` returns it) back to SDK values.

    A one-key object whose key is a tag of :data:`JSONL_TAGS` is converted; a
    one-key object whose key starts with ``$`` but is not a tag is refused, so
    a typo never silently becomes data.

    Args:
        value: The parsed JSON value.
        _line: Line number for error messages.

    Returns:
        The decoded value.

    Raises:
        DataFormatError: On a malformed tag payload or an unknown tag (E3).
    """
    if isinstance(value, dict):
        if len(value) == 1:
            key, payload = next(iter(value.items()))
            if isinstance(key, str) and key.startswith("$"):
                if key not in JSONL_TAGS:
                    raise _format_error(f"unknown tag {key!r}", _line)
                return _decode_tag(key, payload, _line)
        return {key: decode_value(item, _line=_line) for key, item in value.items()}
    if isinstance(value, list):
        return [decode_value(item, _line=_line) for item in value]
    return value


def loads_record(line: str, *, _line: int | None = None) -> dict[str, Any]:
    """
    Read one JSON line back into a record.

    A leading byte order mark is ignored. The JSON is strict: ``NaN`` and
    ``Infinity`` literals are refused (the codec writes ``{"$float": ...}``).

    Args:
        line: One line of text.
        _line: Line number for error messages.

    Returns:
        dict: The decoded record.

    Raises:
        DataFormatError: On invalid JSON, a non-object line, or a malformed
            or unknown tag (E3).
    """
    if line.startswith("﻿"):
        line = line[1:]
    try:
        parsed = json.loads(line, parse_constant=_reject_constant)
    except ValueError as exc:
        raise _format_error(f"invalid JSON ({exc})", _line) from None
    if not isinstance(parsed, dict):
        raise _format_error(f"expected a JSON object, got {_json_type(parsed)}", _line)
    record: dict[str, Any] = decode_value(parsed, _line=_line)
    if not isinstance(record, dict):
        # A one-key tagged object at top level ({"$uuid": ...}) is not a record.
        raise _format_error(f"expected a JSON object, got {type(record).__name__}", _line)
    return record


def _decode_tag(tag: str, payload: Any, line: int | None) -> Any:
    """Convert one tag's payload, or raise E3."""
    if tag == "$object":
        if isinstance(payload, dict):
            return {key: decode_value(item, _line=line) for key, item in payload.items()}
    elif tag == "$record":
        if isinstance(payload, list) and len(payload) == 2 and isinstance(payload[0], str) and payload[0]:
            key = decode_value(payload[1], _line=line)
            if isinstance(key, (str, UUID, list, dict)) or (isinstance(key, int) and not isinstance(key, bool)):
                return RecordId(table=payload[0], id=key)
    elif tag == "$table":
        if isinstance(payload, str) and payload:
            return Table(name=payload)
    elif tag == "$datetime":
        if isinstance(payload, str):
            try:
                parsed = datetime.fromisoformat(payload)
            except ValueError:
                pass
            else:
                return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    elif tag == "$decimal":
        if isinstance(payload, str):
            try:
                number = Decimal(payload)
            except InvalidOperation:
                pass
            else:
                if number.is_finite():
                    return number
    elif tag == "$uuid":
        if isinstance(payload, str):
            try:
                return UUID(payload)
            except ValueError:
                pass
    elif tag == "$duration":
        if isinstance(payload, str) and _DURATION_RE.fullmatch(payload):
            return Duration(value=payload)
        if (
            isinstance(payload, list)
            and len(payload) <= 2
            and all(isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in payload)
        ):
            return Duration(value=tuple(payload))
    elif tag == "$bytes":
        if isinstance(payload, str):
            try:
                return base64.b64decode(payload, validate=True)
            except (binascii.Error, ValueError):
                pass
    elif tag == "$float":
        if isinstance(payload, str) and payload in _FLOAT_TAGS:
            return _FLOAT_TAGS[payload]
    raise _format_error(f"invalid {tag} value: {payload!r}", line)


def _format_error(message: str, line: int | None) -> DataFormatError:
    prefix = "" if line is None else f"Line {line}: "
    return DataFormatError(prefix + message, line=line)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _json_type(value: Any) -> str:
    """The JSON name of a parsed value's type."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"
