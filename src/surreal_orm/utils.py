import re


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
