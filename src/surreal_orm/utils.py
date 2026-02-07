import asyncio
import functools
import logging
import random
import re
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


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

    Example:
        @retry_on_conflict(max_retries=5)
        async def claim_event(event_id: str, pod_id: str):
            await Event.atomic_set_add(event_id, "processed_by", pod_id)
    """

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
