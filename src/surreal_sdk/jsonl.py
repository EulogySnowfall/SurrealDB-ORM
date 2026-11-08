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
``{"$file": [b, key]}``   :class:`~surreal_sdk.files.FileRef` (a file
                          pointer, ``f"b:/key"``)
``{"$object": {…}}``      a one-key object whose key starts with ``$``
========================  =============================================

A value the codec cannot carry losslessly is refused at export with
:class:`~surreal_sdk.exceptions.DataFormatError` rather than written as
something else: a ``tuple`` outside a record id (how the SDK decodes a
geometry, a range or an out-of-range datetime), a ``set``, a non-finite
``Decimal``, any other object. A malformed line is refused at import the same
way, so a typo in a tag never silently becomes data.

``typed=False`` writes plain JSON for external tools (record ids as
``table:id`` text, file pointers as ``bucket:/key`` text, datetimes as ISO
strings, …); such a file reads back as plain JSON, strings staying strings
(:meth:`~surreal_sdk.files.FileRef.parse` reads the pointer text).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import itertools
import json
import math
import os
import re
import time
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import IO, TYPE_CHECKING, Any, Final, Literal, Protocol, TextIO
from uuid import UUID

from .exceptions import DataFormatError, DataImportError, QueryError, SurrealDBError, TableNotFoundError
from .files import FileRef
from .protocol.cbor import Duration, RecordId, Table, _RecordIdRange, _warn_naive_datetime
from .utils import _open_owner_only, format_record_id, require_int, validate_table_name

if TYPE_CHECKING:
    from .connection.base import BaseSurrealConnection

__all__ = [
    "JSONL_TAGS",
    "ImportReport",
    "JsonlDestination",
    "JsonlSource",
    "QueryHook",
    "decode_value",
    "dumps_record",
    "encode_value",
    "loads_record",
]

#: Every tag the codec writes and reads.
JSONL_TAGS: Final[frozenset[str]] = frozenset(
    {"$record", "$table", "$datetime", "$decimal", "$uuid", "$duration", "$bytes", "$float", "$object", "$file"}
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
        lines: Non-blank lines read and decoded. Equal to ``rows`` after a
            successful import; on a :class:`DataImportError` it also counts
            the lines of the failed batch (and a line decoded just before it
            was sent), so it can exceed ``rows``.
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
    if isinstance(value, FileRef):
        return _encode_file(value, typed, path, record)
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


def _encode_file(value: FileRef, typed: bool, path: str, record: Any) -> Any:
    """Encode a file pointer: tagged ``[bucket, key]`` or ``bucket:/key`` text.

    The decoder builds a pointer the server sent without validation (a
    ``memory`` bucket stores ``/../x`` as a plain key). Such a pointer is
    refused here, because the import reads ``$file`` through the validating
    constructor and would reject the line.
    """
    try:
        valid = FileRef(value.bucket, value.key) == value
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise DataFormatError(
            f"Cannot write {_where(path, record)} as JSON Lines: invalid file pointer {str(value)!r}",
            path=path or None,
        )
    return {"$file": [value.bucket, value.key]} if typed else str(value)


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
    elif tag == "$file":
        if isinstance(payload, list) and len(payload) == 2:
            try:
                return FileRef(payload[0], payload[1])
            except (TypeError, ValueError):
                pass
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


# ---------------------------------------------------------------------------
# Batch export
# ---------------------------------------------------------------------------


class SupportsWriteText(Protocol):
    """A text stream: anything with ``write(str)`` (an open file, ``io.StringIO``, …)."""

    def write(self, text: str, /) -> object: ...


#: Where :meth:`~surreal_sdk.connection.base.BaseSurrealConnection.export_jsonl`
#: writes: a path (UTF-8; a new file is created readable by its owner only) or
#: a text stream (left open).
type JsonlDestination = str | os.PathLike[str] | SupportsWriteText

#: Called once per statement sent, after its reply: ``(sql, variables, duration_ms)``.
type QueryHook = Callable[[str, dict[str, Any], float], None]

_RESERVED_PREFIX: Final = "_x_"


def _require_cbor(conn: BaseSurrealConnection, method: str) -> None:
    """E4: JSON Lines needs CBOR, under which ids, datetimes and decimals keep their types."""
    if conn.protocol != "cbor":
        raise ValueError(
            f"{method}() needs the CBOR protocol: under JSON, record ids, datetimes and decimals "
            "arrive as strings and would not round-trip"
        )


def _check_reserved_vars(names: Iterable[str]) -> None:
    """E14: ``$_x_after`` and ``$_x_rows`` are the batch loops' own bindings."""
    for name in names:
        if name.startswith(_RESERVED_PREFIX):
            raise ValueError(f"Variable names starting with '{_RESERVED_PREFIX}' are reserved for data export/import: {name!r}")


def _check_export_args(
    conn: BaseSurrealConnection,
    method: str,
    table: object,
    batch_size: object,
    limit: object,
    vars: Mapping[str, Any] | None,
) -> tuple[str, int, int | None]:
    """Argument checks of the export methods, in the order of §3.2; no I/O."""
    _require_cbor(conn, method)
    name = validate_table_name(table)
    size = require_int("batch_size", batch_size, minimum=1)
    cap = None if limit is None else require_int("limit", limit, minimum=0)
    _check_reserved_vars(vars or {})
    return name, size, cap


def _raise_on_error(response: Any, sql: str) -> None:
    """Raise for the first ``ERR`` result of a reply (a failing ``LET`` prelude included)."""
    for result in response.results:
        if result.is_error:
            message = str(result.result)
            if TableNotFoundError.is_table_not_found(message):
                raise TableNotFoundError(message, query=sql)
            raise QueryError(message, query=sql)


async def _iter_batches(
    conn: BaseSurrealConnection,
    table: str,
    *,
    batch_size: int,
    where_sql: str | None,
    vars: Mapping[str, Any],
    limit: int | None,
    method: str,
    prelude: Sequence[str] = (),
    on_query: QueryHook | None = None,
) -> AsyncGenerator[list[dict[str, Any]], None]:
    """
    Read ``table`` in ``id`` order, one batch per statement (C1/C2).

    The first statement reads ``FROM <table>``. Each next one reads
    ``FROM $_x_after``, bound to the record-id range that starts right after
    the previous batch's last ``id`` (:class:`~surreal_sdk.protocol.cbor._RecordIdRange`,
    ``table:key>..``). The server seeks to that key instead of scanning the
    table from its first record, so every batch costs the same wherever it
    starts; the condition, ``ORDER BY id`` and ``LIMIT`` are unchanged. The
    key is bound, never written into the text, so every kind of record id
    pages the same way, and the cursor record need not exist any more. The
    loop ends on a batch shorter than requested (an empty one included) or
    once ``limit`` records were read. Only the batch being yielded is
    referenced here, so at most one batch is held.

    Args:
        conn: The connection to query.
        table: A validated table name, written into the text.
        batch_size: Rows per statement (``>= 1``).
        where_sql: The condition, already rendered: the SDK's ``({where})``
            or the ORM's filter parts joined with `` AND ``; ``None`` for none.
        vars: Variables bound with every statement.
        limit: The most records to read, ``None`` for all.
        method: The public method's name, for the protocol error (E4).
        prelude: ``LET`` statements sent before each ``SELECT`` (C1p/C2p);
            the records are read from the last result.
        on_query: Called after each reply with the text, the variables and
            the duration in milliseconds.

    Yields:
        list[dict[str, Any]]: The decoded records of one batch, never empty.

    Raises:
        TableNotFoundError: If the table does not exist.
        QueryError: If a statement of the batch comes back ``ERR``.
        SurrealDBError: A failed request propagates unchanged.
    """
    _require_cbor(conn, method)
    head = " ".join(prelude) + " " if prelude else ""
    condition = f"WHERE {where_sql} " if where_sql is not None else ""
    last_id: Any = None
    written = 0
    while True:
        n = batch_size if limit is None else min(batch_size, limit - written)
        if n <= 0:
            return
        if written == 0:
            sql = f"{head}SELECT * FROM {table} {condition}ORDER BY id LIMIT {n};"
            variables = dict(vars)
        else:
            sql = f"{head}SELECT * FROM $_x_after {condition}ORDER BY id LIMIT {n};"
            variables = {**vars, "_x_after": _RecordIdRange(last_id)}
        started = time.perf_counter()
        response = await conn.query(sql, variables)
        if on_query is not None:
            on_query(sql, variables, (time.perf_counter() - started) * 1000.0)
        _raise_on_error(response, sql)
        records = list(response.results[-1].records) if response.results else []
        del response
        if not records:
            return
        written += len(records)
        last_id = records[-1]["id"]
        yield records
        if len(records) < n:
            return
        del records


class _Sink:
    """Text written off the event loop: every blocking call goes through ``asyncio.to_thread``."""

    def __init__(self, stream: SupportsWriteText, owned: IO[str] | None) -> None:
        self._stream = stream
        self._owned = owned

    async def write(self, text: str) -> None:
        await asyncio.to_thread(self._stream.write, text)

    async def close(self) -> None:
        """Close the file if this sink opened it; a caller's stream stays open."""
        if self._owned is not None:
            await asyncio.to_thread(self._owned.close)


async def _open_sink(dest: JsonlDestination) -> _Sink:
    """
    Open ``dest`` for writing JSON Lines.

    A path is opened by :func:`~surreal_sdk.utils._open_owner_only`, like a
    dump (truncated, UTF-8, ``\n`` line ends on every platform, a new file
    created with mode ``0o600``), in a worker thread, and closed by
    :meth:`_Sink.close`; a text stream is written as is and never closed.
    """
    if isinstance(dest, (str, os.PathLike)):
        file = await asyncio.to_thread(_open_owner_only, os.fspath(dest), "w")
        return _Sink(file, file)
    return _Sink(dest, None)


async def _export(
    batches: AsyncGenerator[list[dict[str, Any]], None],
    dest: JsonlDestination,
    *,
    typed: bool,
) -> int:
    """Write each batch as one block of lines; return the number of records written.

    On an error the lines already written stay in ``dest`` and the file is closed.
    """
    sink = await _open_sink(dest)
    count = 0
    try:
        async with contextlib.aclosing(batches):
            async for batch in batches:
                text = "".join(dumps_record(record, typed=typed) + "\n" for record in batch)
                await sink.write(text)
                count += len(batch)
                del batch, text
    finally:
        await sink.close()
    return count


# ---------------------------------------------------------------------------
# Batch import
# ---------------------------------------------------------------------------

#: What :meth:`~surreal_sdk.connection.base.BaseSurrealConnection.import_jsonl`
#: reads: a path (a ``str`` is a path, UTF-8), a text stream (left open), or an
#: iterable / async iterable of lines.
type JsonlSource = str | os.PathLike[str] | TextIO | Iterable[str] | AsyncIterable[str]

#: Lines read per worker-thread call from a file or a text stream.
_READ_BLOCK: Final = 1000

#: ``(INFO FOR DB).tables[...]`` text of a table definition (M47).
_TABLE_TYPE_RE: Final = re.compile(r"^DEFINE TABLE (?:OVERWRITE |IF NOT EXISTS )?\S+ TYPE (ANY|NORMAL|RELATION)\b")

_C10: Final = "RETURN (INFO FOR DB).tables[$_x_table];"


def _check_import_args(
    conn: BaseSurrealConnection,
    table: object,
    batch_size: object,
    max_batch_bytes: object,
    on_conflict: object,
) -> tuple[str, int, int]:
    """Argument checks of ``import_jsonl()``, in the order of §3.2; no I/O."""
    _require_cbor(conn, "import_jsonl")
    name = validate_table_name(table)
    size = require_int("batch_size", batch_size, minimum=1)
    max_bytes = require_int("max_batch_bytes", max_batch_bytes, minimum=1)
    if on_conflict not in ("error", "ignore"):
        raise ValueError(f"on_conflict must be 'error' or 'ignore', got {on_conflict!r}")
    return name, size, max_bytes


def _read_block(stream: IO[str] | TextIO) -> list[str]:
    """Up to :data:`_READ_BLOCK` lines of ``stream`` (run in a worker thread)."""
    return list(itertools.islice(stream, _READ_BLOCK))


async def _iter_source_lines(source: JsonlSource) -> AsyncGenerator[tuple[int, str], None]:
    """
    Yield ``(line number, stripped text)`` for every line of ``source``, from 1.

    A path is opened (UTF-8) and a text stream read in a worker thread, in
    blocks of lines, so the event loop never blocks on the file; the file this
    function opened is closed, a caller's stream is not. An iterable or an
    async iterable gives one line per item (a trailing line break stripped).
    Blank lines are yielded too, so the caller numbers lines as a reader does.
    """
    number = 0
    if isinstance(source, (str, os.PathLike)):
        path = os.fspath(source)

        def open_file() -> IO[str]:
            return open(path, encoding="utf-8")

        file = await asyncio.to_thread(open_file)
        try:
            while block := await asyncio.to_thread(_read_block, file):
                for text in block:
                    number += 1
                    yield number, text.strip()
        finally:
            await asyncio.to_thread(file.close)
    elif hasattr(source, "read"):
        stream: TextIO = source  # type: ignore[assignment]
        while block := await asyncio.to_thread(_read_block, stream):
            for text in block:
                number += 1
                yield number, text.strip()
    elif isinstance(source, AsyncIterable):
        async for text in source:
            number += 1
            yield number, text.strip()
    else:
        for text in source:
            number += 1
            yield number, text.strip()


def _is_edge(row: Mapping[str, Any]) -> bool:
    """A row that ``INSERT RELATION`` can write: ``in`` and ``out`` decoded to record ids."""
    return isinstance(row.get("in"), RecordId) and isinstance(row.get("out"), RecordId)


async def _read_table_type(conn: BaseSurrealConnection, table: str, on_query: QueryHook | None) -> bool | None:
    """
    C10: ``True`` for a ``TYPE RELATION`` table, ``False`` for ``TYPE NORMAL``.

    ``None`` (decide per batch) for ``TYPE ANY``, a missing table, an
    unreadable definition, or an ``ERR`` (a record user may not run ``INFO``).
    """
    variables = {"_x_table": table}
    started = time.perf_counter()
    response = await conn.query(_C10, variables)
    if on_query is not None:
        on_query(_C10, variables, (time.perf_counter() - started) * 1000.0)
    if not response.results or response.results[-1].is_error:
        return None
    text = response.results[-1].result
    match = _TABLE_TYPE_RE.match(text) if isinstance(text, str) else None
    if match is None or match.group(1) == "ANY":
        return None
    return match.group(1) == "RELATION"


async def _insert_batches(
    conn: BaseSurrealConnection,
    table: str,
    lines: AsyncIterator[tuple[int, str]],
    *,
    batch_size: int,
    max_batch_bytes: int,
    on_conflict: Literal["error", "ignore"],
    raw: bool,
    relation: bool | None,
    on_query: QueryHook | None = None,
) -> ImportReport:
    """
    Write decoded lines to ``table``, one ``INSERT`` statement per batch (C3-C7).

    A batch is flushed when it holds ``batch_size`` rows or when the next line
    would take its encoded size past ``max_batch_bytes``; a line larger than
    that is sent alone. One statement is atomic on the server (M25): a batch is
    written whole or not at all, and the report counts only accepted batches.
    A record id of another table is moved to ``table`` (the export of ``e``
    loads into ``e_copy``). With ``relation=None`` the table's type is read once, before the first
    batch (C10).

    Args:
        conn: The connection to write on.
        table: A validated table name, written into the text.
        lines: ``(line number, stripped text)`` pairs; blank texts are skipped.
        batch_size: Rows per statement (``>= 1``).
        max_batch_bytes: Encoded bytes per statement (``>= 1``).
        on_conflict: ``"ignore"`` skips existing ids (``INSERT IGNORE``).
        raw: Prefix ``OPTION IMPORT;`` (no type check, ``VALUE``, event).
        relation: ``True`` for ``INSERT RELATION``, ``False`` for plain,
            ``None`` to decide from the table type or the rows.
        on_query: Called after each reply with the text, a summary of the
            variables (``{"_x_rows": {"rows": n, "bytes": b}}``, never the
            rows) and the duration in milliseconds.

    Returns:
        ImportReport: What was written.

    Raises:
        DataFormatError: On an invalid line (E3) or a batch mixing edges and
            plain rows when the kind is decided per batch (E7).
        DataImportError: When a batch comes back ``ERR`` (E8) or its request
            fails (E8b); ``report`` holds the batches accepted before it
            and every line decoded so far.
    """
    ignore = "IGNORE " if on_conflict == "ignore" else ""
    prefix = "OPTION IMPORT; " if raw else ""
    plain_sql = f"{prefix}INSERT {ignore}INTO {table} $_x_rows RETURN NONE;"
    edge_sql = f"{prefix}INSERT RELATION {ignore}INTO {table} $_x_rows RETURN NONE;"
    decided = relation is not None
    decoded_lines = 0
    accepted_rows = 0
    accepted_batches = 0
    rows: list[dict[str, Any]] = []
    first = last = 0
    size = 0

    def report() -> ImportReport:
        return ImportReport(table=table, lines=decoded_lines, rows=accepted_rows, batches=accepted_batches)

    async def flush() -> None:
        nonlocal rows, size, accepted_rows, accepted_batches, relation, decided
        if not decided:
            relation = await _read_table_type(conn, table, on_query)
            decided = True
        if relation is None:
            edges = sum(1 for row in rows if _is_edge(row))
            if 0 < edges < len(rows):
                raise DataFormatError(
                    f"Lines {first}-{last}: some rows have record 'in' and 'out' fields and some do not; "
                    "pass relation=True or relation=False",
                    line=first,
                )
            sql = edge_sql if edges else plain_sql
        else:
            sql = edge_sql if relation else plain_sql
        started = time.perf_counter()
        try:
            response = await conn.query(sql, {"_x_rows": rows})
        except SurrealDBError as exc:
            raise DataImportError(
                f"Batch at lines {first}-{last} could not be sent to {table} ({type(exc).__name__}: {exc}); "
                "the batches before it are written, this one may or may not be",
                errors=[str(exc)],
                code=exc.code,
                report=report(),
            ) from exc
        if on_query is not None:
            on_query(sql, {"_x_rows": {"rows": len(rows), "bytes": size}}, (time.perf_counter() - started) * 1000.0)
        for result in response.results:
            if result.is_error:
                message = str(result.result)
                raise DataImportError(
                    f"Batch at lines {first}-{last} was rejected by {table}: {message}",
                    errors=[message],
                    report=report(),
                )
        accepted_rows += len(rows)
        accepted_batches += 1
        rows = []
        size = 0

    async for number, text in lines:
        if not text:
            continue
        record = loads_record(text, _line=number)
        decoded_lines += 1
        rid = record.get("id")
        if isinstance(rid, RecordId) and rid.table != table:
            # INSERT RELATION refuses an id of another table; plain INSERT
            # rewrites it to the target (M21). Same rule for both.
            record["id"] = RecordId(table=table, id=rid.id)
        length = len(text.encode("utf-8"))
        if rows and size + length > max_batch_bytes:
            await flush()
        if not rows:
            first = number
        rows.append(record)
        last = number
        size += length
        if len(rows) >= batch_size or size >= max_batch_bytes:
            await flush()
    if rows:
        await flush()
    return report()


async def _import(
    conn: BaseSurrealConnection,
    table: str,
    source: JsonlSource,
    *,
    batch_size: int,
    max_batch_bytes: int,
    on_conflict: Literal["error", "ignore"],
    raw: bool,
    relation: bool | None,
    on_query: QueryHook | None,
) -> ImportReport:
    """Read ``source`` and write it with :func:`_insert_batches`; the source is closed on every exit."""
    lines = _iter_source_lines(source)
    async with contextlib.aclosing(lines):
        return await _insert_batches(
            conn,
            table,
            lines,
            batch_size=batch_size,
            max_batch_bytes=max_batch_bytes,
            on_conflict=on_conflict,
            raw=raw,
            relation=relation,
            on_query=on_query,
        )
