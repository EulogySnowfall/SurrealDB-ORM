import asyncio
import functools
import json
import logging
import random
import re
from collections.abc import Callable, Collection, Mapping
from typing import Any, TypeVar

from surreal_sdk.protocol.cbor import _warn_naive_datetime
from surreal_sdk.utils import find_param_references, substitute_params, unescape_record_id

from .constants import VALID_LOOKUPS

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Shared identifier validation regex — used by query_set, model_base, aggregations, etc.
# Ends with ``\Z``, not ``$``: ``$`` also matches before a final newline, so
# ``"name\n"`` passed ``.match()``. Call sites use ``fullmatch()`` all the same.
SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\Z")


def validate_identifier(name: str, context: str = "identifier") -> None:
    """Validate that a string is a safe SurrealQL identifier.

    Raises:
        ValueError: If the name contains characters outside ``[a-zA-Z0-9_]``
            (whitespace and a trailing newline included) or does not start
            with a letter/underscore.
    """
    if not isinstance(name, str) or not SAFE_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            f"Invalid {context}: {name!r}. "
            "Only letters, digits, and underscores are allowed "
            "(must start with a letter or underscore)."
        )


def validate_field_path(path: str, context: str = "field name") -> None:
    """Validate a field name, or a dotted path into nested objects.

    ``"address.city"`` is accepted: each segment between dots must be an
    identifier (see :func:`validate_identifier`). Nothing else is — no empty
    segment, whitespace, backticks, brackets or ``*``.

    Raises:
        ValueError: If any segment of ``path`` is not an identifier. The
            message names the whole path.
    """
    if not isinstance(path, str) or not all(SAFE_IDENTIFIER_RE.fullmatch(part) for part in path.split(".")):
        raise ValueError(
            f"Invalid {context}: {path!r}. "
            "Expected a field name or a dotted path of field names "
            "(letters, digits and underscores, each starting with a letter or underscore)."
        )


def parse_filter_key(key: str) -> tuple[str, str]:
    """Split a ``filter()`` / ``Q`` key into its field path and lookup.

    ``"age__gte"`` gives ``("age", "gte")``; a key without ``__`` is an
    ``exact`` lookup. The field part is checked with
    :func:`validate_field_path` (dotted paths into nested objects allowed) and
    the lookup must be one the ORM knows, because the field part is written
    into the query as given.

    Raises:
        ValueError: If the field part is not a field name or dotted path, or
            the lookup is unknown. The message names the key.
    """
    if not isinstance(key, str):
        raise ValueError(f"Invalid filter key {key!r}: expected a string.")
    field_name, sep, lookup_name = key.partition("__")
    if not sep:
        lookup_name = "exact"
    try:
        validate_field_path(field_name)
    except ValueError:
        raise ValueError(
            f"Invalid filter key {key!r}: {field_name!r} is not a field name. "
            "Expected a field name or a dotted path of field names, optionally followed by '__<lookup>'."
        ) from None
    if lookup_name not in VALID_LOOKUPS:
        raise ValueError(
            f"Invalid filter key {key!r}: unknown lookup {lookup_name!r}. "
            f"Supported lookups: {', '.join(sorted(VALID_LOOKUPS))}."
        )
    return field_name, lookup_name


def surql_string(text: str) -> str:
    """Render *text* as a single-quoted SurrealQL string literal.

    SurrealQL escapes with a backslash, not by doubling the quote as SQL does:
    ``'it''s'`` is a parse error, and an unescaped backslash starts an escape
    sequence (``'C:\\temp'`` holds a tab). The backslash is escaped first, so
    the backslash added before each apostrophe is not escaped again.

    Args:
        text: The string value

    Returns:
        The quoted literal, read back by the server as *text*
    """
    return "'" + escape_single_quotes(text) + "'"


#: The escape sequences SurrealDB reports inside a string literal.
_SURQL_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f"}
_SURQL_ESCAPE_RE = re.compile(r"\\(?:u\{([0-9A-Fa-f]{1,6})\}|u([0-9A-Fa-f]{4})|(.))", re.DOTALL)


def _decode_surql_escape(match: re.Match[str]) -> str:
    """Decode one backslash escape of a SurrealQL string literal."""
    code = match.group(1) or match.group(2)
    if code is not None:
        return chr(int(code, 16))
    char = match.group(3)
    # An escape SurrealDB does not define is kept as written.
    return _SURQL_ESCAPES.get(char, "\\" + char)


def parse_surql_string(raw: str) -> str:
    """Read back a SurrealQL string literal as the server reports it.

    SurrealDB reports a string in single quotes unless it contains an
    apostrophe, in which case it uses double quotes. Either way the content
    carries backslash escapes (``\\"``, ``\\\\``, ``\\n``...), decoded here in
    one pass. The inverse of :func:`surql_string`.

    Args:
        raw: The literal, quotes included

    Returns:
        The string value; *raw* stripped but otherwise unchanged when it is
        not a quoted literal
    """
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in "'\"" and raw[-1] == raw[0]:
        return _SURQL_ESCAPE_RE.sub(_decode_surql_escape, raw[1:-1])
    return raw


def escape_single_quotes(value: str) -> str:
    """Escape *value* for the inside of a single-quoted SurrealQL string literal.

    Backslashes become ``\\\\``, then apostrophes ``\\'``; SurrealQL has no
    doubled-quote escape. :func:`surql_string` adds the quotes.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


#: A whole value naming one query variable: ``$`` then an identifier, nothing else.
_VARIABLE_REFERENCE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


def is_declared_variable_reference(value: Any, declared: Collection[str]) -> bool:
    """
    Tell whether a filter value is a reference to a declared query variable.

    A filter value is normally bound as a ``$_fN`` parameter. It is written
    into the query text as a variable reference instead only when both hold:

    - the whole value is ``$`` followed by an identifier. ``re.fullmatch`` is
      deliberate: ``re.match`` with a ``$`` anchor would also accept a trailing
      newline, and anything else after the name would end up in the query text;
    - the name is in ``declared``: a variable passed through
      ``QuerySet.variables()``. Names the ORM binds while compiling
      (``$_fN``, ...) are not declared.

    Any other value, including ``"$name"`` for an undeclared name, is data.

    Args:
        value: The filter value as supplied by the caller.
        declared: The variable names the query will be sent with.

    Returns:
        True if the value should be rendered as a variable reference.

    Examples:
        is_declared_variable_reference("$min_age", {"min_age"})    # True
        is_declared_variable_reference("$min_age", set())           # False
        is_declared_variable_reference("$min_age\\n", {"min_age"})  # False
    """
    return isinstance(value, str) and _VARIABLE_REFERENCE_RE.fullmatch(value) is not None and value[1:] in declared


def remove_quotes_for_variables(query: str) -> str:
    # Regex for remove single cote on variables ($)
    return re.sub(r"'(\$[a-zA-Z_]\w*)'", r"\1", query)


def needs_id_escaping(record_id: str) -> bool:
    """
    Check if a record ID needs to be escaped in SurrealQL.

    Record IDs that start with a digit or contain special characters
    need to be wrapped in backticks or Unicode angle brackets.

    Args:
        record_id: The record ID string (without table prefix)

    Returns:
        True if the ID needs escaping, False otherwise

    Examples:
        needs_id_escaping("abc123")  # False - starts with letter
        needs_id_escaping("7abc")    # True - starts with digit
        needs_id_escaping("test-id") # True - contains hyphen
        needs_id_escaping("test.id") # True - contains dot
    """
    if not record_id:
        return False

    # IDs starting with a digit need escaping
    if record_id[0].isdigit():
        return True

    # IDs containing special characters need escaping
    # Valid unescaped characters: letters, digits, underscore
    # SurrealDB allows alphanumeric and underscore without escaping
    if not SAFE_IDENTIFIER_RE.fullmatch(record_id):
        return True

    return False


def escape_record_id(record_id: str) -> str:
    r"""
    Escape a record ID for use in SurrealQL if needed.

    Uses SurrealDB's backtick-delimited form for IDs that contain special
    characters or start with a digit. Inside that form SurrealDB reads ``\``
    as an escape character, so a backslash is written ``\\`` and a backtick
    ``\``` — the backslash first, otherwise the backslash added in front of a
    backtick would itself be doubled.

    This is the only place a record ID is turned into SurrealQL text:
    :func:`format_thing` and every query builder go through it.
    :func:`surreal_sdk.utils.unescape_record_id` reads the result back.

    Args:
        record_id: The record ID string (without table prefix)

    Returns:
        The escaped record ID (with backticks if needed)

    Examples:
        escape_record_id("abc123")   # "abc123" - no escaping needed
        escape_record_id("7abc")     # "`7abc`" - escaped
        escape_record_id("test-id")  # "`test-id`" - escaped
        escape_record_id("a`b")      # "`a\`b`"
        escape_record_id("dir\\")    # "`dir\\`"
    """
    if needs_id_escaping(record_id):
        escaped = record_id.replace("\\", "\\\\").replace("`", "\\`")
        return f"`{escaped}`"
    return record_id


def format_thing(table: str, record_id: str) -> str:
    """
    Format a full SurrealDB thing reference (table:id).

    Properly escapes the record ID if it contains special characters
    or starts with a digit.

    Args:
        table: The table name
        record_id: The record ID (without table prefix)

    Returns:
        The formatted thing reference

    Examples:
        format_thing("users", "abc123")  # "users:abc123"
        format_thing("users", "7abc")    # "users:`7abc`"
        format_thing("game_tables", "7qvdzsc14e5clo8sg064")
        # "game_tables:`7qvdzsc14e5clo8sg064`"
    """
    escaped_id = escape_record_id(record_id)
    return f"{table}:{escaped_id}"


def parse_record_id(full_id: str) -> tuple[str | None, str]:
    """
    Parse a full record ID (table:id) into table and id parts.

    Handles backtick-escaped IDs, as :func:`format_thing` writes them, through
    :func:`surreal_sdk.utils.unescape_record_id`.

    Args:
        full_id: Full record ID in format "table:id" or just "id"

    Returns:
        Tuple of (table_name, id) where table_name may be None if not present

    Examples:
        parse_record_id("users:abc123")  # ("users", "abc123")
        parse_record_id("users:`7abc`")  # ("users", "7abc")
        parse_record_id("abc123")        # (None, "abc123")
    """
    if ":" not in full_id:
        return None, full_id

    table, id_part = full_id.split(":", 1)

    return table, unescape_record_id(id_part)


def _is_complex_value(value: Any) -> bool:
    """Check if a value is a complex nested dict/list that may fail CBOR variable binding.

    A value is considered "complex" if it's a dict containing any nested dict
    or list values, or a list containing dicts.  These structures can trigger
    SurrealDB v2.6 CBOR parameter-binding issues (GitHub Issue #55).
    """
    if isinstance(value, dict):
        for v in value.values():
            if isinstance(v, (dict, list)):
                return True
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return True
    return False


class _SurrealJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles datetime, Decimal, UUID for SurrealQL inlining."""

    def default(self, o: Any) -> Any:
        from datetime import date, datetime, time
        from decimal import Decimal
        from uuid import UUID

        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, time):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, UUID):
            return str(o)
        return super().default(o)


#: A ``__SURQL_DT_N__`` placeholder, including the JSON quotes around it.
_DATETIME_MARKER = re.compile(r'"(__SURQL_DT_\d+__)"')


def _extract_datetime_values(
    value: Any,
    markers: dict[str, str],
    counter: list[int],
) -> Any:
    """Replace ``datetime`` objects with placeholder markers for inline JSON.

    Walks the value recursively.  Each ``datetime`` found is replaced with a
    unique ``__SURQL_DT_N__`` placeholder string and the corresponding
    SurrealQL ``d"<iso>"`` literal is stored in *markers*.  After
    ``json.dumps()``, the caller replaces ``"__SURQL_DT_N__"`` (including
    the surrounding JSON quotes) with the unwrapped literal.

    A naive ``datetime`` is read as UTC, with the same ``RuntimeWarning`` the
    CBOR encoder emits, so both paths store the same instant: SurrealDB
    refuses a ``d"..."`` literal with no offset.
    """
    from datetime import UTC, datetime

    if isinstance(value, datetime):
        if value.tzinfo is None:
            _warn_naive_datetime()
            value = value.replace(tzinfo=UTC)
        marker = f"__SURQL_DT_{counter[0]}__"
        counter[0] += 1
        markers[marker] = f'd"{value.isoformat()}"'
        return marker
    if isinstance(value, dict):
        return {k: _extract_datetime_values(v, markers, counter) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        converted = [_extract_datetime_values(item, markers, counter) for item in value]
        return type(value)(converted)
    return value


def _expand_datetime_markers(json_str: str, markers: Mapping[str, str]) -> str:
    """Swap each quoted ``__SURQL_DT_N__`` placeholder for its ``d"..."`` literal.

    Takes *markers* as a parameter rather than closing over the caller's loop
    variable, which ruff flags (B023) and which would be a real bug if the
    callable outlived the iteration.
    """
    return _DATETIME_MARKER.sub(lambda match: markers[match.group(1)], json_str)


def inline_dict_variables(
    query: str,
    variables: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Replace complex dict/list variables with inline JSON in a SurrealQL query.

    For each variable that contains a deeply nested dict or list,
    the ``$name`` reference in the query string is replaced with the JSON
    literal and the variable is removed from the bindings dict.

    This works around SurrealDB v2.6 CBOR variable-binding limitations
    with complex nested objects (GitHub Issue #55).

    .. warning::

        Inlined values bypass CBOR parameter binding.  Only use this with
        trusted, application-generated data — never with raw user input.

    Args:
        query: SurrealQL query string with ``$variable`` references.
        variables: Variables dict. Not mutated — a new dict is returned.

    Returns:
        ``(modified_query, remaining_variables)`` tuple.

    Raises:
        ValueError: If a referenced complex value cannot be serialized.
    """
    # Which names the query actually uses. Without this a complex value whose
    # reference is absent — or not a valid identifier, such as ``$my-var`` —
    # would be serialized for nothing and then dropped from both the query and
    # the returned bindings, silently.
    referenced = find_param_references(query)

    inlined: dict[str, str] = {}
    remaining: dict[str, Any] = {}
    for key, value in variables.items():
        if key not in referenced or not _is_complex_value(value):
            remaining[key] = value
            continue

        # Extract datetime objects as markers so they become d"..." literals
        dt_markers: dict[str, str] = {}
        counter = [0]
        processed = _extract_datetime_values(value, dt_markers, counter)

        try:
            # ensure_ascii=False keeps astral characters raw. The default emits
            # them as UTF-16 surrogate pairs (\ud83d\ude42), which SurrealDB 3.x
            # rejects at parse time: "String contains invalid escape sequence".
            json_str = json.dumps(processed, cls=_SurrealJSONEncoder, ensure_ascii=False)
            # ensure_ascii=False also lets a lone surrogate through json.dumps;
            # encoding here keeps the failure inside the promised ValueError
            # instead of surfacing as a UnicodeEncodeError in the CBOR encoder.
            json_str.encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as e:
            raise ValueError(f"Failed to serialize variable '{key}' to JSON for inlining: {e}") from e

        # Replace datetime marker strings (with JSON quotes) with unwrapped
        # SurrealQL d"..." literals — one pass, for the same reason the query
        # substitution is one pass: a str.replace per marker rescans the whole
        # payload, which is quadratic in the number of datetimes.
        if dt_markers:
            json_str = _expand_datetime_markers(json_str, dt_markers)

        inlined[key] = json_str

    # One pass over the original query: substituting key by key would re-scan
    # the JSON just inserted, so a "$b" inside a string value of $a would be
    # replaced too, with the outcome depending on dict order.
    return substitute_params(query, inlined), remaining


def retry_on_conflict(
    max_retries: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 2.0,
    backoff_factor: float = 2.0,
) -> Callable[[F], F]:
    """Decorator that retries an async function on transaction conflict errors.

    Uses exponential backoff with jitter to avoid thundering herd problems
    in multi-pod deployments.

    Args:
        max_retries: Maximum number of retry attempts (default: 3).
        base_delay: Initial delay in seconds between retries (default: 0.05).
        max_delay: Maximum delay in seconds (default: 2.0).
        backoff_factor: Multiplier for delay on each retry (default: 2.0).

    Returns:
        Decorated async function with retry logic.

    Raises:
        ValueError: If any parameter is negative or zero (for delays/factor).

    Example:
        @retry_on_conflict(max_retries=5)
        async def claim_event(event_id: str, pod_id: str):
            await Event.atomic_set_add(event_id, "processed_by", pod_id)
    """
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")
    if base_delay <= 0:
        raise ValueError(f"base_delay must be > 0, got {base_delay}")
    if max_delay <= 0:
        raise ValueError(f"max_delay must be > 0, got {max_delay}")
    if backoff_factor <= 0:
        raise ValueError(f"backoff_factor must be > 0, got {backoff_factor}")

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            from surreal_sdk.exceptions import SurrealDBError, TransactionConflictError

            last_error: SurrealDBError | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except SurrealDBError as e:
                    if not TransactionConflictError.is_conflict_error(e):
                        raise
                    last_error = e
                    if attempt < max_retries:
                        delay = min(
                            base_delay * (backoff_factor**attempt),
                            max_delay,
                        )
                        jitter = delay * random.uniform(0.5, 1.0)
                        logger.warning(
                            "Transaction conflict on %s (attempt %d/%d), retrying in %.3fs...",
                            func.__name__,
                            attempt + 1,
                            max_retries,
                            jitter,
                        )
                        await asyncio.sleep(jitter)

            raise TransactionConflictError(
                f"Transaction conflict persisted after {max_retries} retries in {func.__name__}: {last_error}"
            )

        return wrapper  # type: ignore[return-value]

    return decorator
