"""
Shared helpers for building SurrealQL query strings.

Lives in the SDK because both layers need it and the dependency only runs one
way: ``surreal_orm`` imports from ``surreal_sdk``, never the reverse.
"""

import os
import re
from collections.abc import Mapping
from typing import IO, Any, Literal, NamedTuple, overload
from uuid import UUID

from .protocol.cbor import RecordId

__all__ = [
    "escape_record_id",
    "find_param_references",
    "format_record_id",
    "needs_id_escaping",
    "require_int",
    "substitute_params",
    "unescape_record_id",
    "validate_table_name",
]

#: A table, relation or field name SurrealQL reads without delimiters, and a
#: record ID that needs no escaping. Always used with ``fullmatch``: ``$``
#: would also accept a trailing newline.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A ``$name`` reference. ``\w+`` is greedy on purpose: it captures the whole
#: identifier, so ``$state`` cannot match the prefix of ``$state_backup``.
#: ``re.ASCII`` matches SurrealDB's own identifier lexer, which is ASCII — an
#: accented name is not a reference the server would bind either.
_PARAM_REFERENCE = re.compile(r"\$(\w+)", re.ASCII)


@overload
def _open_owner_only(path: str, mode: Literal["wb"]) -> IO[bytes]: ...


@overload
def _open_owner_only(path: str, mode: Literal["w"]) -> IO[str]: ...


def _open_owner_only(path: str, mode: Literal["wb", "w"]) -> IO[Any]:
    """
    Open ``path`` for writing database data: truncated, created with mode ``0o600`` when new.

    A dump or a table export holds the database's data (password hashes,
    access keys, tokens), so a new file is readable by its owner only, whatever
    the umask. An existing file is truncated and keeps its mode (the caller
    chose it). On Windows the mode is ignored.

    Every SDK path that writes database data to a file goes through here:
    ``HTTPConnection.export_database()`` (``mode="wb"``) and the JSON Lines
    export (``mode="w"``: UTF-8, ``\\n`` line ends on every platform).

    Args:
        path: The file to create or truncate.
        mode: ``"wb"`` for a binary stream, ``"w"`` for UTF-8 text.

    Returns:
        IO[bytes] | IO[str]: The open file; the caller closes it.

    Raises:
        ValueError: If ``mode`` is neither ``"wb"`` nor ``"w"``.
        OSError: If the file cannot be opened.
    """
    if mode not in ("wb", "w"):
        raise ValueError(f"mode must be 'wb' or 'w', not {mode!r}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if mode == "wb":
            return os.fdopen(fd, "wb")
        return os.fdopen(fd, "w", encoding="utf-8", newline="")
    except BaseException:
        os.close(fd)
        raise


def require_int(name: str, value: object, *, minimum: int) -> int:
    """
    Check that ``value`` is an integer of at least ``minimum`` and return it.

    The one integer check of both layers: the ORM's pagination, KNN and
    search-reference arguments are written into the query text, and the SDK's
    sizes (``WebSocketConnection(max_message_size=)``) must be real numbers.
    Only an ``int`` is accepted: a string (even a numeric one), a float or a
    ``bool`` raises ``TypeError``, and a value below ``minimum`` raises
    ``ValueError``. The result is always a plain ``int``, so an ``int``
    subclass with its own ``__format__`` renders as its numeric value.

    Args:
        name: Argument name, used in the error message.
        value: The value passed by the caller.
        minimum: The smallest accepted value.

    Returns:
        int: ``value`` as a plain ``int``.

    Raises:
        TypeError: If ``value`` is not an ``int`` or is a ``bool``.
        ValueError: If ``value`` is below ``minimum``.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    number = int.__index__(value)
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {number}")
    return number


def find_param_references(query: str) -> set[str]:
    """Return the ``$name`` references that appear in *query*.

    Lets a caller tell "this parameter is used" from "this parameter is not",
    which :func:`substitute_params` alone cannot: an unmatched entry in its
    replacements is indistinguishable from one that matched.

    Args:
        query: SurrealQL possibly containing ``$name`` references

    Returns:
        The referenced names, without the leading ``$``
    """
    return set(_PARAM_REFERENCE.findall(query))


def substitute_params(query: str, replacements: Mapping[str, str]) -> str:
    r"""Replace ``$name`` references with pre-rendered text, in a single pass.

    Two properties matter, and both are easy to lose by hand:

    **The text is inserted verbatim.** ``re.sub`` reads a replacement *string*
    as a mini-pattern — ``\1`` is a backreference, ``\g<0>`` a group reference,
    and an unknown escape is an error. Rendered SurrealQL is full of
    backslashes (JSON doubles every literal one), so passing it as a string
    raises on some inputs and silently collapses backslashes on others. A
    *callable* replacement is exempt from that parsing.

    **It is one pass over the original query.** Substituting one key at a time
    re-scans text that was just inserted, so a ``$b`` occurring inside a string
    value of ``$a`` gets replaced too — and which one wins depends on mapping
    order.

    A reference with no entry in *replacements* is left as it was, so callers
    can inline some parameters and leave the rest as bindings.

    .. note::

        The scan is textual: a ``$name`` written inside a quoted SurrealQL
        string literal is substituted like any other reference.

    Args:
        query: SurrealQL containing ``$name`` references
        replacements: Reference name to the exact text that replaces it

    Returns:
        The query with every known reference substituted

    Examples:
        substitute_params("SET a = $a", {"a": '{"x": 1}'})   # 'SET a = {"x": 1}'
        substitute_params("SET a = $a", {})                  # 'SET a = $a'
    """
    return _PARAM_REFERENCE.sub(lambda match: replacements.get(match.group(1), match.group(0)), query)


#: One unit of a backtick-delimited record ID body: an escaped backslash or
#: backtick, a doubled backtick (the form older releases wrote), or any
#: other character.
_ESCAPED_ID_UNIT = re.compile(r"\\[\\`]|``|.", re.DOTALL)


def unescape_record_id(id_part: str) -> str:
    r"""Read back a record ID written by :func:`escape_record_id`.

    A backtick-delimited ID loses its delimiters, ``\\`` becomes ``\`` and
    ``\``` becomes a backtick. Those are the only two sequences the escaper
    produces, so every other backslash is kept as written. A doubled backtick
    is still read as one, so a string produced by the older escaper parses as
    it always did. An ID that is not backtick-delimited is returned unchanged.

    Args:
        id_part: The record ID part of a ``table:id`` reference

    Returns:
        The raw record ID
    """
    if len(id_part) < 2 or not (id_part.startswith("`") and id_part.endswith("`")):
        return id_part
    return _ESCAPED_ID_UNIT.sub(lambda m: m.group(0)[-1] if len(m.group(0)) == 2 else m.group(0), id_part[1:-1])


#: A record ID part that SurrealQL reads as an integer when written inline
#: (``users:1``, ``users:-5``), provided it fits in a signed 64-bit integer;
#: outside that range the server reads it as a string ID.
_INTEGER_ID_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

#: ID generators SurrealQL accepts after ``table:`` (``CREATE users:ulid()``).
_ID_GENERATORS = frozenset({"rand()", "ulid()", "uuid()"})


class _RecordReference(NamedTuple):
    """A ``table`` or ``table:id`` reference, as :func:`_parse_record_reference` reads it."""

    #: The table, an identifier.
    table: str
    #: The record ID: an ``int`` in the signed 64-bit range, a ``str`` otherwise,
    #: ``None`` for a bare table.
    id: int | str | None
    #: True when the ID was written backtick- or angle-bracket-delimited.
    delimited: bool = False

    @property
    def generator(self) -> bool:
        """True for an undelimited ``rand()``, ``ulid()`` or ``uuid()``, which SurrealQL evaluates."""
        return not self.delimited and self.id in _ID_GENERATORS


def _delimited_record_id(id_part: str, thing: str, context: str) -> str | None:
    r"""Return the raw ID of a delimited ID part, ``None`` when it is not delimited.

    Two delimited forms are read back: backticks, as ``format_thing()`` writes
    them (``\\`` and ``\``` inside), and Unicode angle brackets without
    escapes. A delimited form that is not closed, or that holds its own closing
    delimiter without an escape, raises ``ValueError``.
    """
    if id_part.startswith("`"):
        units = _ESCAPED_ID_UNIT.findall(id_part[1:-1]) if len(id_part) >= 2 and id_part.endswith("`") else ["`"]
        if "`" in units:
            raise ValueError(f"Invalid {context}: {thing!r} (malformed backtick-delimited ID)")
        return unescape_record_id(id_part)
    if id_part.startswith("\u27e8"):
        body = id_part[1:-1]
        if len(id_part) < 2 or not id_part.endswith("\u27e9") or "\u27e9" in body or "\\" in body:
            raise ValueError(f"Invalid {context}: {thing!r} (malformed angle-bracket ID)")
        return body
    return None


def _parse_record_reference(thing: object, context: str) -> _RecordReference:
    """Read a ``table`` or ``table:id`` reference the way SurrealQL reads it inline.

    The single reading of a record reference in the SDK: ``HTTPTransaction``
    renders its statements from it and both connections build the ``RecordId``
    of their RPC calls from it, so one string names one record on every path.

    The table must be an identifier. The ID, after the first ``:``, is read as:

    * an ``int`` when it is an integer in canonical form within the signed
      64-bit range (``users:1``, ``users:-5``); outside that range, or with a
      leading ``+`` or ``0``, the server reads the digits as a string ID;
    * the raw string of a backtick-delimited ID (as ``format_thing()`` writes
      it, read back once) or of a Unicode angle-bracket-delimited ID
      (``users:\u27e8a b\u27e9``), never itself an integer;
    * the text as written otherwise, ``rand()``/``ulid()``/``uuid()`` included
      (see :attr:`_RecordReference.generator`).

    Array and object IDs are not accepted.

    Args:
        thing: The reference.
        context: The argument name, for the error message.

    Returns:
        The table, the ID (``None`` for a bare table) and whether it was delimited.

    Raises:
        ValueError: If the reference is not a string, the table is not an
            identifier, the ID is empty, a delimiter is not closed, or the ID is
            an array or an object.
    """
    if not isinstance(thing, str):
        raise ValueError(f"Invalid {context}: {thing!r} (expected a 'table' or 'table:id' string)")
    table, sep, id_part = thing.partition(":")
    if not _IDENTIFIER_RE.fullmatch(table):
        raise ValueError(f"Invalid {context}: {thing!r} (the table must be an identifier)")
    if not sep:
        return _RecordReference(table, None)
    if not id_part:
        raise ValueError(f"Invalid {context}: {thing!r} (empty record ID)")
    raw = _delimited_record_id(id_part, thing, context)
    if raw is not None:
        if not raw:
            raise ValueError(f"Invalid {context}: {thing!r} (empty record ID)")
        return _RecordReference(table, raw, delimited=True)
    if len(id_part) <= 20 and _INTEGER_ID_RE.fullmatch(id_part) and _I64_MIN <= int(id_part) <= _I64_MAX:
        return _RecordReference(table, int(id_part))
    if id_part[0] in "[{":
        raise ValueError(f"Invalid {context}: {thing!r} (array and object IDs are not supported; use query())")
    return _RecordReference(table, id_part)


def _rpc_record_id(thing: str) -> tuple[str, int | str]:
    """Return the ``(table, id)`` of a ``table:id`` reference passed to an RPC method.

    Reads ``thing`` with :func:`_parse_record_reference`. An RPC method takes a
    record ID, not SurrealQL, so an ID generator is refused: the server would
    store the text ``"ulid()"`` as the ID instead of generating one.

    Raises:
        ValueError: If the reference is malformed, has no ID, or its ID is a generator.
    """
    ref = _parse_record_reference(thing, "thing")
    if ref.id is None:
        raise ValueError(f"Invalid thing: {thing!r} (expected a 'table:id' reference)")
    if ref.generator:
        raise ValueError(
            f"Invalid thing: {thing!r} (an ID generator is SurrealQL, not a record ID; "
            "pass the table name for a random ID, or use query())"
        )
    return ref.table, ref.id


def validate_table_name(name: object) -> str:
    """
    Check that ``name`` is a table name SurrealQL reads without delimiters.

    The rule of the transaction builders: an ASCII identifier, matched in full
    (a trailing newline is refused). Used wherever a table name is written into
    the query text rather than bound.

    Args:
        name: The table name passed by the caller.

    Returns:
        str: ``name``, unchanged.

    Raises:
        ValueError: If ``name`` is not a string or not an identifier.
    """
    if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Invalid table name: {name!r} (expected an identifier)")
    return name


def needs_id_escaping(record_id: str) -> bool:
    """
    Check if a record ID needs to be escaped in SurrealQL.

    Record IDs that start with a digit or contain anything but ASCII letters,
    digits and underscores are written backtick-delimited.

    Args:
        record_id: The record ID string (without table prefix)

    Returns:
        True if the ID needs escaping, False otherwise

    Examples:
        needs_id_escaping("abc123")  # False - starts with letter
        needs_id_escaping("7abc")    # True - starts with digit
        needs_id_escaping("test-id") # True - contains hyphen
    """
    if not record_id:
        return False
    if record_id[0].isdigit():
        return True
    return not _IDENTIFIER_RE.fullmatch(record_id)


def escape_record_id(record_id: str) -> str:
    r"""
    Escape a record ID for use in SurrealQL if needed.

    Uses SurrealDB's backtick-delimited form for IDs that contain special
    characters or start with a digit. Inside that form SurrealDB reads ``\``
    as an escape character, so a backslash is written ``\\`` and a backtick
    ``\``` — the backslash first, otherwise the backslash added in front of a
    backtick would itself be doubled.

    This is the only place a record ID is turned into SurrealQL text:
    :func:`format_record_id`, ``surreal_orm.utils.format_thing`` and every
    query builder go through it. :func:`unescape_record_id` reads the result
    back.

    Args:
        record_id: The record ID string (without table prefix)

    Returns:
        The escaped record ID (with backticks if needed)

    Examples:
        escape_record_id("abc123")   # "abc123" - no escaping needed
        escape_record_id("7abc")     # "`7abc`" - escaped
        escape_record_id("a`b")      # "`a\`b`"
    """
    if needs_id_escaping(record_id):
        escaped = record_id.replace("\\", "\\\\").replace("`", "\\`")
        return f"`{escaped}`"
    return record_id


def format_record_id(rid: RecordId) -> str:
    """
    Write a decoded record ID as ``table:key`` text the server reads back.

    The key is written by type: an ``int`` as its digits, a ``str`` through
    :func:`escape_record_id` (backtick-delimited when it is not an
    identifier), a ``UUID`` as ``u'…'``, and an array or object key as compact
    JSON. Never use ``str(RecordId)`` for text that is read back: it drops the
    key's escaping.

    Args:
        rid: The record ID, as the SDK decodes it.

    Returns:
        str: The ``table:key`` text.

    Raises:
        DataFormatError: If an array or object key holds a value JSON Lines
            cannot carry.

    Examples:
        format_record_id(RecordId("person", "alice"))   # "person:alice"
        format_record_id(RecordId("person", "7 x"))     # "person:`7 x`"
        format_record_id(RecordId("person", [1, "a"]))  # 'person:[1,"a"]'
    """
    key = rid.id
    if isinstance(key, int) and not isinstance(key, bool):
        text = str(int(key))
    elif isinstance(key, str):
        text = escape_record_id(key)
    elif isinstance(key, UUID):
        text = f"u'{key}'"
    else:
        # Imported here: the codec imports this module.
        from .jsonl import _relaxed_key_json

        text = _relaxed_key_json(key)
    return f"{rid.table}:{text}"
