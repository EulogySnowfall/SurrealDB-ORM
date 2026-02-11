"""
Parser for SurrealDB DEFINE statements returned by INFO commands.

Parses the string representations of DEFINE TABLE, DEFINE FIELD,
DEFINE INDEX, and DEFINE ACCESS statements into structured data
compatible with the migration state classes (FieldState, IndexState, etc.).
"""

from __future__ import annotations

import re
from typing import Any

from .state import FieldState, IndexState

# Keywords that delimit clauses in a DEFINE FIELD statement.
# Order matters: longer keywords first to avoid partial matches.
_FIELD_KEYWORDS = [
    "PERMISSIONS",
    "FLEXIBLE",
    "READONLY",
    "COMMENT",
    "DEFAULT",
    "ASSERT",
    "VALUE",
    "TYPE",
]

# Keywords that delimit clauses in a DEFINE TABLE statement.
_TABLE_KEYWORDS = [
    "SCHEMAFULL",
    "SCHEMALESS",
    "CHANGEFEED",
    "PERMISSIONS",
    "COMMENT",
    "TYPE",
    "AS",
    "DROP",
]


def _extract_clauses(body: str, keywords: list[str]) -> dict[str, str]:
    """Extract keyword-delimited clauses from a DEFINE statement body.

    Scans the body string left-to-right, splitting on recognized keywords.
    Each keyword's value extends until the next keyword or end-of-string.

    Args:
        body: The statement body after ``DEFINE FIELD name ON table``
              or ``DEFINE TABLE name``.
        keywords: Ordered list of recognized keywords.

    Returns:
        Dict mapping keyword (uppercase) to its value string (stripped).
        Boolean keywords like READONLY/FLEXIBLE have an empty string value.
    """
    clauses: dict[str, str] = {}
    remaining = body.strip()

    while remaining:
        remaining = remaining.strip()
        if not remaining:
            break

        matched = False
        for kw in keywords:
            if remaining.upper().startswith(kw):
                after = remaining[len(kw) :]
                # Must be followed by whitespace, end-of-string, or be a boolean keyword
                if after == "" or after[0] in (" ", "\t", "\n"):
                    # Find where the next keyword starts
                    value, rest = _consume_until_keyword(after.strip(), keywords)
                    clauses[kw] = value.strip()
                    remaining = rest
                    matched = True
                    break

        if not matched:
            # Skip unknown token
            parts = remaining.split(None, 1)
            remaining = parts[1] if len(parts) > 1 else ""

    return clauses


def _consume_until_keyword(text: str, keywords: list[str]) -> tuple[str, str]:
    """Consume text until the next recognized keyword boundary.

    Respects parentheses, single-quoted strings, and angle brackets
    so that expressions like ``string::concat(first_name, ' ', last_name)``
    or ``array<string>`` are not split prematurely.

    Returns:
        (consumed_value, remaining_text)
    """
    i = 0
    depth_paren = 0
    depth_angle = 0
    in_quote = False

    while i < len(text):
        ch = text[i]

        if ch == "'" and not in_quote:
            in_quote = True
            i += 1
            continue
        if ch == "'" and in_quote:
            # Check for escaped quote
            if i > 0 and text[i - 1] == "\\":
                i += 1
                continue
            in_quote = False
            i += 1
            continue
        if in_quote:
            i += 1
            continue

        if ch == "(":
            depth_paren += 1
        elif ch == ")":
            depth_paren -= 1
        elif ch == "<":
            depth_angle += 1
        elif ch == ">":
            depth_angle -= 1

        # Only check for keyword boundaries at depth 0
        if depth_paren == 0 and depth_angle == 0:
            upper_rest = text[i:].upper()
            for kw in keywords:
                if upper_rest.startswith(kw):
                    after_kw = text[i + len(kw) :]
                    if after_kw == "" or after_kw[0] in (" ", "\t", "\n"):
                        return text[:i], text[i:]

        i += 1

    return text, ""


def parse_define_field(statement: str) -> FieldState:
    """Parse a DEFINE FIELD statement into a FieldState.

    Examples::

        parse_define_field("DEFINE FIELD email ON users TYPE string")
        parse_define_field(
            "DEFINE FIELD full_name ON users TYPE string "
            "VALUE string::concat(first_name, ' ', last_name)"
        )

    Args:
        statement: Full DEFINE FIELD statement string (with or without
                   trailing semicolon).

    Returns:
        Populated FieldState instance.
    """
    stmt = statement.strip().rstrip(";").strip()

    # Extract field name and table from the prefix
    # Format: DEFINE FIELD [IF NOT EXISTS|OVERWRITE] <name> ON [TABLE] <table> ...
    match = re.match(
        r"DEFINE\s+FIELD\s+(?:IF\s+NOT\s+EXISTS\s+|OVERWRITE\s+)?(\S+)\s+ON\s+(?:TABLE\s+)?(\S+)\s*(.*)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"Cannot parse DEFINE FIELD statement: {statement!r}")

    field_name = match.group(1)
    # table_name = match.group(2)  # Not stored in FieldState
    body = match.group(3)

    clauses = _extract_clauses(body, _FIELD_KEYWORDS)

    field_type = clauses.get("TYPE", "any")
    field_type_lower = field_type.lower()
    nullable = field_type_lower.startswith("option<") or "| null" in field_type_lower

    # Normalize: unwrap option<T> to T (the nullable flag carries the info)
    if field_type_lower.startswith("option<") and field_type.endswith(">"):
        field_type = field_type[7:-1].strip()
    flexible = "FLEXIBLE" in clauses
    readonly = "READONLY" in clauses
    encrypted = False

    # Parse VALUE clause
    value_expr = clauses.get("VALUE") or None
    if value_expr and "crypto::argon2::generate" in value_expr:
        encrypted = True
        value_expr = None  # Don't store the crypto expression as a computed value

    # Parse DEFAULT clause
    default = _parse_default_value(clauses.get("DEFAULT"))

    # Parse ASSERT clause
    assertion = clauses.get("ASSERT") or None

    return FieldState(
        name=field_name,
        field_type=field_type,
        nullable=nullable,
        default=default,
        assertion=assertion,
        encrypted=encrypted,
        flexible=flexible,
        readonly=readonly,
        value=value_expr,
    )


def _parse_default_value(raw: str | None) -> Any:
    """Parse a DEFAULT clause value into a Python object."""
    if raw is None or raw == "":
        return None

    raw = raw.strip()

    # None / NONE / NULL
    if raw.upper() in ("NONE", "NULL"):
        return None

    # Boolean
    if raw.lower() == "true":
        return True
    if raw.lower() == "false":
        return False

    # Quoted string
    if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
        return raw[1:-1]

    # Integer
    try:
        return int(raw)
    except ValueError:
        pass

    # Float
    try:
        return float(raw)
    except ValueError:
        pass

    # Function call or expression (e.g., time::now())
    return raw


def parse_define_table(statement: str) -> dict[str, Any]:
    """Parse a DEFINE TABLE statement into a dict.

    Returns a dict with keys compatible with TableState:
    ``name``, ``schema_mode``, ``table_type``, ``changefeed``,
    ``permissions``.

    Args:
        statement: Full DEFINE TABLE statement string.

    Returns:
        Dict with parsed table properties.
    """
    stmt = statement.strip().rstrip(";").strip()

    match = re.match(
        r"DEFINE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+|OVERWRITE\s+)?(\S+)\s*(.*)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"Cannot parse DEFINE TABLE statement: {statement!r}")

    table_name = match.group(1)
    body = match.group(2)

    clauses = _extract_clauses(body, _TABLE_KEYWORDS)

    # Schema mode
    schema_mode = "SCHEMAFULL"
    if "SCHEMALESS" in clauses:
        schema_mode = "SCHEMALESS"
    elif "SCHEMAFULL" in clauses:
        schema_mode = "SCHEMAFULL"

    # Table type from TYPE clause (e.g., TYPE NORMAL, TYPE RELATION)
    table_type = "normal"
    if "TYPE" in clauses:
        raw_type = clauses["TYPE"].strip().upper()
        if raw_type.startswith("RELATION"):
            table_type = "relation"
        elif raw_type == "ANY":
            table_type = "any"
        else:
            table_type = raw_type.lower()

    # Changefeed
    changefeed = clauses.get("CHANGEFEED") or None
    if changefeed:
        changefeed = changefeed.split()[0]  # Just the duration part

    # Permissions
    permissions = _parse_permissions(clauses.get("PERMISSIONS"))

    return {
        "name": table_name,
        "schema_mode": schema_mode,
        "table_type": table_type,
        "changefeed": changefeed,
        "permissions": permissions,
    }


def _parse_permissions(raw: str | None) -> dict[str, str]:
    """Parse a PERMISSIONS clause into a dict of action → condition.

    Handles formats like:
    - ``FULL`` → {}
    - ``NONE`` → {"select": "NONE", "create": "NONE", ...}
    - ``FOR select WHERE $auth.id = id FOR update WHERE $auth.id = id``
    """
    if raw is None or raw == "":
        return {}

    raw = raw.strip()
    if raw.upper() == "FULL":
        return {}
    if raw.upper() == "NONE":
        return {
            "select": "NONE",
            "create": "NONE",
            "update": "NONE",
            "delete": "NONE",
        }

    permissions: dict[str, str] = {}
    # Match: FOR <action[, action]> WHERE <condition>
    for m in re.finditer(
        r"FOR\s+([\w\s,]+?)\s+WHERE\s+(.+?)(?=\s+FOR\s+|\s*$)",
        raw,
        re.IGNORECASE,
    ):
        actions_str = m.group(1).strip()
        condition = m.group(2).strip()
        for action in re.split(r"[,\s]+", actions_str):
            action = action.strip().lower()
            if action:
                permissions[action] = condition

    return permissions


def parse_define_index(statement: str) -> IndexState:
    """Parse a DEFINE INDEX statement into an IndexState.

    Handles standard, UNIQUE, SEARCH ANALYZER (with optional BM25 / HIGHLIGHTS),
    and HNSW vector index definitions.

    Args:
        statement: Full DEFINE INDEX statement string.

    Returns:
        Populated IndexState instance.
    """
    stmt = statement.strip().rstrip(";").strip()

    match = re.match(
        r"DEFINE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+|OVERWRITE\s+)?(\S+)\s+ON\s+(?:TABLE\s+)?(\S+)\s+(?:FIELDS|COLUMNS)\s+(.+)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"Cannot parse DEFINE INDEX statement: {statement!r}")

    index_name = match.group(1)
    rest = match.group(3).strip()

    # Keywords that delimit the fields list from index-type flags
    _INDEX_KEYWORDS = [
        "UNIQUE",
        "SEARCH",
        "HNSW",
        "DIMENSION",
        "DIST",
        "TYPE",
        "EFC",
        "BM25",
        "HIGHLIGHTS",
        "CONCURRENTLY",
        "COMMENT",
        "MTREE",
    ]

    # Find where the fields list ends by looking for the first keyword
    # at a proper word boundary (both before and after the keyword).
    upper_rest = rest.upper()
    fields_end = len(rest)
    for kw in _INDEX_KEYWORDS:
        pos = 0
        while pos < len(upper_rest):
            idx = upper_rest.find(kw, pos)
            if idx == -1:
                break
            # Check word boundary before the keyword
            before_ok = idx == 0 or upper_rest[idx - 1] in (" ", "\t", "\n", ",")
            # Check word boundary after the keyword
            after_idx = idx + len(kw)
            after_ok = after_idx >= len(upper_rest) or upper_rest[after_idx] in (" ", "\t", "\n", "(")
            if before_ok and after_ok and idx < fields_end:
                fields_end = idx
                break
            pos = idx + 1

    fields_str = rest[:fields_end].strip().rstrip(",")
    fields = [f.strip() for f in fields_str.split(",") if f.strip()]

    flags_str = rest[fields_end:]

    # Strip COMMENT clause before keyword detection so that comment
    # text like 'COMMENT "Uses HNSW for speed"' doesn't cause false
    # matches on keywords like UNIQUE, BM25, or HNSW.
    flags_str = re.sub(r"""COMMENT\s+(?:"[^"]*"|'[^']*')""", "", flags_str, flags=re.IGNORECASE)

    upper_flags = flags_str.upper()

    # ── Standard flags ──────────────────────────────────────────────
    unique = "UNIQUE" in upper_flags

    # ── Full-text search ────────────────────────────────────────────
    search_analyzer: str | None = None
    search_match = re.search(r"SEARCH\s+ANALYZER\s+(\S+)", flags_str, re.IGNORECASE)
    if search_match:
        search_analyzer = search_match.group(1)

    bm25: tuple[float, float] | bool | None = None
    bm25_match = re.search(r"BM25\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*\)", flags_str, re.IGNORECASE)
    if bm25_match:
        bm25 = (float(bm25_match.group(1)), float(bm25_match.group(2)))
    elif "BM25" in upper_flags:
        bm25 = True

    highlights = "HIGHLIGHTS" in upper_flags

    # ── HNSW vector index ───────────────────────────────────────────
    hnsw = "HNSW" in upper_flags

    # Vector-specific parameters are only meaningful for HNSW indexes.
    # Parsing them for other index types (e.g. MTREE) would populate
    # IndexState fields that later generate invalid CreateIndex SQL.
    dimension: int | None = None
    dist: str | None = None
    vector_type: str | None = None
    efc: int | None = None
    m_val: int | None = None

    if hnsw:
        dim_match = re.search(r"DIMENSION\s+(\d+)", flags_str, re.IGNORECASE)
        if dim_match:
            dimension = int(dim_match.group(1))

        dist_match = re.search(r"DIST\s+(\S+)", flags_str, re.IGNORECASE)
        if dist_match:
            dist = dist_match.group(1).upper()

        type_match = re.search(r"\bTYPE\s+(\S+)", flags_str, re.IGNORECASE)
        if type_match:
            vector_type = type_match.group(1).upper()

        efc_match = re.search(r"EFC\s+(\d+)", flags_str, re.IGNORECASE)
        if efc_match:
            efc = int(efc_match.group(1))

        m_match = re.search(r"\bM\s+(\d+)", flags_str, re.IGNORECASE)
        if m_match:
            m_val = int(m_match.group(1))

    concurrently = "CONCURRENTLY" in upper_flags

    return IndexState(
        name=index_name,
        fields=fields,
        unique=unique,
        search_analyzer=search_analyzer,
        bm25=bm25,
        highlights=highlights,
        hnsw=hnsw,
        dimension=dimension,
        dist=dist,
        vector_type=vector_type,
        efc=efc,
        m=m_val,
        concurrently=concurrently,
    )


def parse_define_access(statement: str) -> dict[str, Any]:
    """Parse a DEFINE ACCESS statement into a dict.

    Returns a dict with keys compatible with AccessState:
    ``name``, ``table``, ``signup_fields``, ``signin_where``,
    ``duration_token``, ``duration_session``.

    Args:
        statement: Full DEFINE ACCESS statement string.

    Returns:
        Dict with parsed access properties.
    """
    stmt = statement.strip().rstrip(";").strip()

    # Extract access name
    match = re.match(
        r"DEFINE\s+ACCESS\s+(?:IF\s+NOT\s+EXISTS\s+|OVERWRITE\s+)?(\S+)\s+ON\s+DATABASE\s+TYPE\s+RECORD\s*(.*)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"Cannot parse DEFINE ACCESS statement: {statement!r}")

    access_name = match.group(1)
    body = match.group(2).strip()

    # Extract SIGNUP clause — content inside balanced parentheses
    signup_fields: dict[str, str] = {}
    table = ""
    signup_match = re.search(r"SIGNUP\s*\(", body, re.IGNORECASE)
    if signup_match:
        start = signup_match.end()
        content = _extract_balanced_parens(body, start - 1)
        # Parse: CREATE <table> SET field1 = $expr1, field2 = $expr2
        create_match = re.match(r"CREATE\s+(\S+)\s+SET\s+(.*)", content, re.IGNORECASE | re.DOTALL)
        if create_match:
            table = create_match.group(1)
            sets_str = create_match.group(2)
            for part in _split_set_clauses(sets_str):
                eq_idx = part.find("=")
                if eq_idx != -1:
                    field_name = part[:eq_idx].strip()
                    field_expr = part[eq_idx + 1 :].strip()
                    signup_fields[field_name] = field_expr

    # Extract SIGNIN clause
    signin_where = ""
    signin_match = re.search(r"SIGNIN\s*\(", body, re.IGNORECASE)
    if signin_match:
        content = _extract_balanced_parens(body, signin_match.end() - 1)
        # Parse: SELECT * FROM <table> WHERE <condition>
        where_match = re.search(r"WHERE\s+(.*)", content, re.IGNORECASE | re.DOTALL)
        if where_match:
            signin_where = where_match.group(1).strip()

    # Extract DURATION clause
    duration_token = "15m"
    duration_session = "12h"
    duration_match = re.search(
        r"DURATION\s+FOR\s+TOKEN\s+(\S+)\s*,\s*FOR\s+SESSION\s+(\S+)",
        body,
        re.IGNORECASE,
    )
    if duration_match:
        duration_token = duration_match.group(1)
        duration_session = duration_match.group(2)

    return {
        "name": access_name,
        "table": table,
        "signup_fields": signup_fields,
        "signin_where": signin_where,
        "duration_token": duration_token,
        "duration_session": duration_session,
    }


def _extract_balanced_parens(text: str, start: int) -> str:
    """Extract content inside balanced parentheses starting at ``text[start]``.

    Args:
        text: Full text.
        start: Index of the opening ``(``.

    Returns:
        Content between the outermost parentheses (exclusive).
    """
    if text[start] != "(":
        raise ValueError(f"Expected '(' at position {start}, got {text[start]!r}")

    depth = 0
    i = start
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : i]
        i += 1

    return text[start + 1 :]


def _split_set_clauses(sets_str: str) -> list[str]:
    """Split SET clause assignments respecting parentheses.

    ``"email = $email, password = crypto::argon2::generate($password)"``
    → ``["email = $email", "password = crypto::argon2::generate($password)"]``
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []

    for ch in sets_str:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)

    if current:
        parts.append("".join(current).strip())

    return parts


def parse_define_analyzer(statement: str) -> dict[str, Any]:
    """Parse a DEFINE ANALYZER statement into a dict.

    Returns a dict with keys compatible with ``AnalyzerState``:
    ``name``, ``tokenizers``, ``filters``.

    Examples::

        parse_define_analyzer(
            "DEFINE ANALYZER my_analyzer TOKENIZERS blank, class "
            "FILTERS lowercase, snowball(english)"
        )
        # → {"name": "my_analyzer",
        #    "tokenizers": ["blank", "class"],
        #    "filters": ["lowercase", "snowball(english)"]}

    Args:
        statement: Full DEFINE ANALYZER statement string.

    Returns:
        Dict with parsed analyzer properties.
    """
    stmt = statement.strip().rstrip(";").strip()

    match = re.match(
        r"DEFINE\s+ANALYZER\s+(?:IF\s+NOT\s+EXISTS\s+|OVERWRITE\s+)?(\S+)\s*(.*)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"Cannot parse DEFINE ANALYZER statement: {statement!r}")

    analyzer_name = match.group(1)
    body = match.group(2).strip()

    tokenizers: list[str] = []
    filters: list[str] = []

    # Extract TOKENIZERS clause
    tok_match = re.search(r"TOKENIZERS\s+(.+?)(?:\s+FILTERS\b|\s+COMMENT\b|$)", body, re.IGNORECASE)
    if tok_match:
        raw = tok_match.group(1).strip()
        tokenizers = [t.strip() for t in raw.split(",") if t.strip()]

    # Extract FILTERS clause (may contain parenthesized args like snowball(english))
    filt_match = re.search(r"FILTERS\s+(.+?)(?:\s+COMMENT\b|$)", body, re.IGNORECASE)
    if filt_match:
        filters = _split_set_clauses(filt_match.group(1).strip())

    return {
        "name": analyzer_name,
        "tokenizers": tokenizers,
        "filters": filters,
    }


__all__ = [
    "parse_define_field",
    "parse_define_table",
    "parse_define_index",
    "parse_define_access",
    "parse_define_analyzer",
]
