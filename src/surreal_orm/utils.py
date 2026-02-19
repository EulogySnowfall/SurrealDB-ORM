import asyncio
import functools
import json
import logging
import random
import re
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# Shared identifier validation regex — used by query_set, model_base, aggregations, etc.
SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def validate_identifier(name: str, context: str = "identifier") -> None:
    """Validate that a string is a safe SurrealQL identifier.

    Raises:
        ValueError: If the name contains characters outside ``[a-zA-Z0-9_]``
            or does not start with a letter/underscore.
    """
    if not SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid {context}: {name!r}. "
            "Only letters, digits, and underscores are allowed "
            "(must start with a letter or underscore)."
        )


def escape_single_quotes(value: str) -> str:
    """Escape single quotes for embedding in SurrealQL string literals.

    SurrealDB uses doubled single quotes (``''``) for escaping inside
    single-quoted strings.
    """
    return value.replace("'", "''")


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
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", record_id):
        return True

    return False


def escape_record_id(record_id: str) -> str:
    """
    Escape a record ID for use in SurrealQL if needed.

    Uses SurrealDB's backtick escaping format for IDs that contain
    special characters or start with a digit.

    Args:
        record_id: The record ID string (without table prefix)

    Returns:
        The escaped record ID (with backticks if needed)

    Examples:
        escape_record_id("abc123")   # "abc123" - no escaping needed
        escape_record_id("7abc")     # "`7abc`" - escaped
        escape_record_id("test-id")  # "`test-id`" - escaped
    """
    if needs_id_escaping(record_id):
        # Escape any backticks within the ID by doubling them
        escaped = record_id.replace("`", "``")
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

    Handles escaped IDs with backticks.

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

    # Handle backtick-escaped IDs
    if id_part.startswith("`") and id_part.endswith("`"):
        # Remove backticks and unescape doubled backticks
        id_part = id_part[1:-1].replace("``", "`")

    return table, id_part


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

    def default(self, obj: Any) -> Any:
        from datetime import date, datetime, time
        from decimal import Decimal
        from uuid import UUID

        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, time):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, UUID):
            return str(obj)
        return super().default(obj)


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
    """
    from datetime import UTC, datetime

    if isinstance(value, datetime):
        if value.tzinfo is None:
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
    """
    remaining: dict[str, Any] = {}
    for key, value in variables.items():
        if _is_complex_value(value):
            # Extract datetime objects as markers so they become d"..." literals
            dt_markers: dict[str, str] = {}
            counter = [0]
            processed = _extract_datetime_values(value, dt_markers, counter)

            try:
                json_str = json.dumps(processed, cls=_SurrealJSONEncoder)
            except (TypeError, ValueError) as e:
                raise ValueError(f"Failed to serialize variable '{key}' to JSON for inlining: {e}") from e

            # Replace datetime marker strings (with JSON quotes) with
            # unwrapped SurrealQL d"..." literals.
            for marker, literal in dt_markers.items():
                json_str = json_str.replace(f'"{marker}"', literal)

            # Replace $key with inline JSON (word-boundary to avoid partial matches)
            query = re.sub(rf"\${re.escape(key)}\b", json_str, query)
        else:
            remaining[key] = value
    return query, remaining


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
