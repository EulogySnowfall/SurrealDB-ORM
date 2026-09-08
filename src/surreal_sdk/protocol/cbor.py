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
- TAG_DATETIME (12): DateTime (ISO 8601)
- TAG_STRING_DURATION (14): Duration as string
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
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
    return not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", record_id)


def escape_record_id(record_id: str) -> str:
    """
    Escape a record ID for SurrealQL when it needs it.

    Args:
        record_id: The record ID, without the table prefix

    Returns:
        The ID, backtick-wrapped when required
    """
    if needs_id_escaping(record_id):
        return "`" + record_id.replace("`", "``") + "`"
    return record_id


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
    """
    if data is None:
        return CBORTag(TAG_NONE, None)
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
    """
    if isinstance(value, RecordId):
        # Encode RecordId as tagged array [table, id]
        encoder.encode(CBORTag(TAG_RECORDID, [value.table, value.id]))
    elif isinstance(value, Table):
        # Encode Table as tagged string
        encoder.encode(CBORTag(TAG_TABLE, value.name))
    elif isinstance(value, datetime):
        # Encode datetime as tagged ISO 8601 string
        # Ensure timezone-aware for consistency
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        encoder.encode(CBORTag(TAG_DATETIME, value.isoformat()))
    elif isinstance(value, UUID):
        # Encode UUID as tagged string
        encoder.encode(CBORTag(TAG_STRING_UUID, str(value)))
    elif isinstance(value, Decimal):
        # Encode Decimal as tagged string to preserve precision
        encoder.encode(CBORTag(TAG_STRING_DECIMAL, str(value)))
    elif isinstance(value, Duration):
        # Encode Duration as tagged string
        encoder.encode(CBORTag(TAG_STRING_DURATION, value.value))
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
        # Parse ISO 8601 datetime string
        if isinstance(tag.value, str):
            return datetime.fromisoformat(tag.value.replace("Z", "+00:00"))
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

    Args:
        data: Python object to encode

    Returns:
        CBOR-encoded bytes
    """
    processed = _preprocess_for_cbor(data)
    result: bytes = cbor2.dumps(processed, default=_cbor_default_encoder)
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
