"""
Parser for SurrealDB DEFINE statements returned by INFO commands.

Parses the string representations of DEFINE TABLE, DEFINE FIELD,
DEFINE INDEX, and DEFINE ACCESS statements into structured data
compatible with the migration state classes (FieldState, IndexState, etc.).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from ..surreal_function import SurrealFunc, _surreal_func_from_server
from .operations import _effective_field_permissions
from .state import EventState, FieldState, IndexState

# Keywords that delimit clauses in a DEFINE FIELD statement.
# Order matters: longer keywords first to avoid partial matches.
# PERMISSIONS is not one: the clause is split off first
# (_split_field_permissions), so its conditions are never scanned for the other
# keywords, and an expression naming a field ``permissions`` is not cut short.
_FIELD_KEYWORDS = [
    "FLEXIBLE",
    "READONLY",
    "COMMENT",
    "DEFAULT",
    "ASSERT",
    "VALUE",
    "TYPE",
]

# Where a field's PERMISSIONS clause starts: the keyword, at the start of the
# body or after whitespace, then FULL, NONE or FOR — the only three ways the
# clause can begin. A field *named* ``permissions`` in a VALUE or ASSERT
# expression is not followed by one.
_FIELD_PERMISSIONS_START = re.compile(r"(?:^|(?<=\s))PERMISSIONS\s+(?:FULL|NONE|FOR)\b", re.IGNORECASE)

# What a ``/`` followed by whitespace must come after to open a regex literal
# rather than divide: an operator or an opening bracket, where an operand is
# expected. A division has an operand on its left — a name, a number, a
# closing bracket or quote — never one of these.
_REGEX_AFTER = frozenset("=([{,:!<>+-|&?")

# Type names that take parameters in angle brackets: ``array<string>``,
# ``option<record<user>>``, ``set<int, 10>``, ``geometry<point>``,
# ``references<user>``. Only a ``<`` written right after one of them opens a
# bracket.
_GENERIC_TYPES = frozenset(
    {"array", "set", "option", "record", "references", "geometry", "either", "range", "future", "function"}
)

# Type names a cast can start with: ``<int> $value``, ``<array<int>> $v``.
_CAST_TYPES = _GENERIC_TYPES | frozenset(
    {
        "any",
        "bool",
        "bytes",
        "datetime",
        "decimal",
        "duration",
        "float",
        "int",
        "none",
        "null",
        "number",
        "object",
        "point",
        "regex",
        "string",
        "uuid",
    }
)

# A DEFINE FIELD clause keyword that takes an expression, at the end of the
# text before a ``/``: an operand is expected there, so the ``/`` can only
# open a pattern (``ASSERT /x/``), while ``$value/2`` divides.
_FIELD_EXPRESSION_KEYWORD_END = re.compile(r"(?:^|(?<=\s))(?:ASSERT|VALUE|DEFAULT)\Z", re.IGNORECASE)

# A word operator, or a keyword that a value follows, at the end of the text
# before a ``/``: an operand is expected there too, so the ``/`` can only open
# a pattern (``$value CONTAINS /x/``). SurrealDB 2.7.0 accepts a pattern after
# each of these. The word must start the text or follow whitespace, so a name
# that ends in one (``$value.in``, ``x_in``) stays an operand on the left.
_WORD_OPERATOR_END = re.compile(
    r"(?:^|(?<=\s))(?:"
    r"CONTAINS(?:NOT|ALL|ANY|NONE)?|(?:NOT|ALL|ANY|NONE)?INSIDE|OUTSIDE|INTERSECTS"
    r"|(?:NOT\s+)?IN|IS(?:\s+NOT)?|AND|OR|THEN|ELSE|RETURN"
    r")\Z",
    re.IGNORECASE,
)

# The four actions a table PERMISSIONS clause can govern, in the order
# SurrealDB reports them.
_PERMISSION_ACTIONS = ("select", "create", "update", "delete")

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


#: What a backslash escape inside a SurrealQL string stands for. Any other
#: escaped character stands for itself (``\\'``, ``\\"``, ``\\\\``).
_STRING_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def _unquote_surql_string(raw: str) -> str:
    """Decode a quoted SurrealQL string as the server reports it.

    The server echoes a text single-quoted, or double-quoted when the text
    holds an apostrophe, and escapes with a backslash: ``"it's \\"x\\""``,
    ``'C:\\\\temp'``, ``'line\\n'``. A text that is not quoted is returned
    stripped and otherwise unchanged.

    Args:
        raw: The literal, quotes included

    Returns:
        The text it stands for
    """
    raw = raw.strip()
    if len(raw) < 2 or raw[0] not in ("'", '"') or raw[-1] != raw[0]:
        return raw
    body = raw[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            escaped = body[i + 1]
            out.append(_STRING_ESCAPES.get(escaped, escaped))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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

    Only a keyword at the top level of the text counts (see
    :func:`_top_level`), so ``string::concat(first_name, ' ', last_name)``,
    ``array<string>``, ``COMMENT "it's"`` or ``/^[A-Za-z' -]+$/`` is not
    split, while ``$value > 0 COMMENT 'positive'`` is.

    Returns:
        (consumed_value, remaining_text)
    """
    for i in _top_level(text):
        # Only check where a clause could actually start: after whitespace.
        # Without the left-hand check a keyword appearing inside an expression
        # ended the clause — `PERMISSIONS FOR select WHERE meta.type != NONE`
        # was cut at `type`, leaving the condition as `meta.` and the table
        # type as `!= none`.
        if i == 0 or text[i - 1] in (" ", "\t", "\n"):
            upper_rest = text[i : i + 16].upper()
            for kw in keywords:
                if upper_rest.startswith(kw):
                    after_kw = text[i + len(kw) :]
                    if after_kw == "" or after_kw[0] in (" ", "\t", "\n"):
                        return text[:i], text[i:]

    return text, ""


def _opens_regex_literal(text: str, i: int, previous: str) -> bool:
    """Whether the ``/`` at *i* opens a regex literal (``/pattern/``).

    SurrealDB renders a division as `` / ``, with whitespace on both sides,
    so a ``/`` followed by anything else opens a regex. A pattern may itself
    start with a space (``/ x/``); that one is told apart by what precedes it.

    Args:
        text: The text being scanned
        i: The index of the ``/``
        previous: The last non-whitespace character before it, outside any
            quoted token (empty at the start of the text)

    Returns:
        True for a regex literal, False for a division
    """
    following = text[i + 1 : i + 2]
    if following and not following.isspace():
        return True
    return previous in _REGEX_AFTER


def _regex_literal_end(text: str, start: int) -> int:
    """Return the index just past the regex literal opening at *start*.

    Read as SurrealDB's lexer reads it: ``\\/`` is an escaped slash, while a
    backslash before anything else escapes nothing — 2.7.0 rejects ``/a\\\\/``
    as unterminated.

    Args:
        text: The text being scanned
        start: The index of the opening ``/``

    Returns:
        The index after the closing ``/``, or -1 when the literal never closes
    """
    i = start + 1
    while i < len(text):
        if text[i] == "\\" and text.startswith("/", i + 1):
            i += 2
        elif text[i] == "/":
            return i + 1
        else:
            i += 1
    return -1


def _quoted_end(text: str, start: int) -> int:
    """Return the index just past the string or identifier quoted at *start*.

    A single-quoted, double-quoted or backtick-quoted token closes at its own
    quote character, and a backslash escapes the character after it, as
    SurrealDB reports them. An unterminated one runs to the end of the text.

    Args:
        text: The text being scanned
        start: The index of the opening quote

    Returns:
        The index after the closing quote, or ``len(text)``
    """
    quote = text[start]
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text[i] == quote:
            return i + 1
        else:
            i += 1
    return len(text)


def _word_before(text: str, end: int) -> str:
    """Return the identifier ending right before *end* (empty when none)."""
    i = end
    while i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
        i -= 1
    return text[i:end]


def _type_bracket_end(text: str, start: int) -> int:
    """Return the index just past the type brackets opening at *start*.

    A ``<`` opens type brackets in two places only: right after a type name
    that takes parameters (``array<string>``, ``option<record<user>>``), and
    as a cast, a ``<`` followed at once by a type name where an operand is
    expected (``<int> $value``, ``<array<int>> $value``). Anywhere else it is
    an operator (``$value < 10``, ``<-follows``), and so is every ``>``
    outside type brackets (``$value > 0``, ``->likes``).

    Args:
        text: The text being scanned
        start: The index of the ``<``

    Returns:
        The index after the matching ``>``, or -1 when *start* is an operator
    """
    if _word_before(text, start).lower() not in _GENERIC_TYPES:
        cast_type = re.match(r"[a-z]+", text[start + 1 :], re.IGNORECASE)
        if cast_type is None or cast_type.group(0).lower() not in _CAST_TYPES:
            return -1
        if not _operand_expected_at(text, start):
            return -1
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            i = _quoted_end(text, i)
            continue
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return i + 1
        elif not (ch.isalnum() or ch in " \t\n_,|:.-{}[]"):
            # Not something a type is written with: `$`, `(`, `=`, `;`
            return -1
        i += 1
    return -1


def _top_level(text: str) -> Iterator[int]:
    """Yield the index of every character of *text* at the top level.

    The clause scanner shared by :func:`_consume_until_keyword` (and so the
    clause splitter) and :func:`_split_permission_groups`. A character is at
    the top level when it sits outside any string or quoted identifier
    (single, double or backtick quotes, with backslash escapes), regex literal
    (``/…/``, read with the 2.x rule of :func:`_regex_literal_end`; a ``/``
    where an operand is expected opens one, even followed by a space, as in
    ``ASSERT / it's/ = $value``), type
    brackets (``array<…>``, a ``<type>`` cast) and ``()``, ``[]`` or ``{}``
    pair. The brackets themselves are not yielded.

    A ``<`` or ``>`` used as an operator is yielded like any other character:
    counting it as a bracket left ``ASSERT $value > 0`` open, which hid every
    clause after it (#568).

    Args:
        text: The text to scan

    Yields:
        Indices into *text*, in increasing order
    """
    depth = 0
    previous = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            i = _quoted_end(text, i)
            previous = ch
            continue
        if ch == "/" and (_opens_regex_literal(text, i, previous) or _operand_expected_at(text, i)):
            end = _regex_literal_end(text, i)
            if end != -1:
                previous = "/"
                i = end
                continue
        elif ch == "<":
            end = _type_bracket_end(text, i)
            if end != -1:
                previous = ">"
                i = end
                continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            yield i
        if not ch.isspace():
            previous = ch
        i += 1


def _scan_field_body(text: str, *, find_clause: bool) -> tuple[int, bool, int]:
    """Scan a DEFINE FIELD body (or part of one) token by token.

    Quotes (``'``, ``"`` and backticks, with backslash escapes), regex
    literals, parentheses, brackets and braces are respected. A pattern is
    not SurrealQL: ``/^[A-Za-z' -]+$/`` or ``/^\\(x/`` would otherwise open a
    quote or a bracket that never closes. Angle brackets are not tracked: they
    only nest inside a type, where the keyword cannot appear, while
    ``ASSERT $value > 0`` would never close.

    A ``/`` read as a division must be written as SurrealDB writes one,
    `` / `` with an operand after it. Any other is the closing ``/`` of a
    pattern, and leaves the text unbalanced unless an earlier `` / `` read
    as a division can be its start: a pattern starting with a space after a
    keyword (``WHERE / x/``) is read that way, and the server reports it so.
    Without such a start, the pattern opened before *text* starts.

    Args:
        text: The text to scan
        find_clause: Stop at the first PERMISSIONS clause start found outside
            any quote, pattern or bracket

    Returns:
        ``(index, balanced, open_pattern)``: the index of that clause start
        (-1 when none was found or none was looked for), whether the text
        ends with no quote, pattern or bracket left open and no pattern end
        without its start, and the index of the ``/`` opening a pattern that
        never closes (-1 when there is none, or when any earlier ``/`` was
        seen at all, whether read as a division, as a stray closer or as
        part of a consumed pattern: that ``/`` can belong to a misread
        pattern, as in ``ASSERT ($value CONTAINS / a+/)``, ``/ a\\/ b/`` or
        ``($value/2 + / a+/)``, which makes the later ``/`` its end rather
        than an opener)
    """
    depth = 0
    quote: str | None = None
    escaped = False
    stray = False
    divisions = 0
    seen_slash = False
    previous = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
                previous = ch
            i += 1
            continue
        if ch == "/" and _opens_regex_literal(text, i, previous):
            end = _regex_literal_end(text, i)
            if end == -1:
                return -1, False, (i if not seen_slash else -1)
            seen_slash = True
            previous = "/"
            i = end
            continue
        if ch == "/":
            seen_slash = True
            if i > 0 and text[i - 1].isspace() and text[i + 1 :].strip():
                divisions += 1
            elif divisions:
                divisions -= 1
            else:
                stray = True
        if ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif find_clause and depth == 0 and ch in "Pp" and _FIELD_PERMISSIONS_START.match(text, i):
            return i, True, -1
        if not ch.isspace():
            previous = ch
        i += 1
    return -1, quote is None and depth == 0 and not stray, -1


def _operand_expected_at(text: str, i: int) -> bool:
    """Whether the ``/`` at *i* sits where an operand is expected.

    It does after a symbol operator or an opening bracket
    (:data:`_REGEX_AFTER`), at the start of *text*, and, followed by
    whitespace, after a clause keyword that takes an expression
    (:data:`_FIELD_EXPRESSION_KEYWORD_END`) or a word operator such as
    ``CONTAINS``, ``IN``, ``NOT IN``, ``INSIDE``, ``AND``, ``OR``, ``THEN``
    or ``RETURN`` (:data:`_WORD_OPERATOR_END`). Anything else — a name, a
    number, a closing bracket or quote — is an operand on its left, so the
    ``/`` can be a division written without spaces (``$value/2``, ``1/2``),
    which SurrealDB accepts. A word written with no whitespace before the
    ``/`` (``$value CONTAINS/2``), or one that only ends a longer name
    (``$value.in``, ``x_in``), is read as such an operand. A field written
    bare with an operator's name (``in`` on a relation table) before an
    unspaced ``/`` is read as the operator.

    Args:
        text: The text being scanned
        i: The index of the ``/``

    Returns:
        True when only a pattern can start at *i*
    """
    before = text[:i]
    stripped = before.rstrip()
    if not stripped or stripped[-1] in _REGEX_AFTER:
        return True
    if len(stripped) == len(before):
        return False
    return _FIELD_EXPRESSION_KEYWORD_END.search(stripped) is not None or _WORD_OPERATOR_END.search(stripped) is not None


def _split_field_permissions(body: str) -> tuple[str, str]:
    """Split a DEFINE FIELD body at its PERMISSIONS clause.

    SurrealDB always reports a field's PERMISSIONS clause last, and its
    ``WHERE`` conditions name fields unescaped — ``FOR select WHERE type =
    'public'``. Scanned with the other clauses, ``type`` there was read as a
    TYPE clause, and the ``>`` of a preceding ``ASSERT $value > 0`` hid the
    PERMISSIONS keyword so the whole clause ended up in the assertion (#259).
    The scan (:func:`_scan_field_body`) skips quotes, regex literals and
    brackets.

    When the scan finds no clause, the candidate starts are tried from the
    last one backwards: the server reports a PERMISSIONS clause for every
    field, and always last. A start is kept only when what follows its
    keyword ends balanced under the same scan; one inside a string literal
    (``DEFAULT 'x PERMISSIONS FULL'``) leaves the quote open and is skipped
    (#287), and one inside a regex literal (``ASSERT $value = / PERMISSIONS
    FULL/``) leaves the pattern's closing ``/`` without its start (#509).
    A start is also skipped when the text before it ends inside a pattern
    whose opening ``/`` is followed by a non-space character, which cannot be
    a division: in ``ASSERT $value = /x PERMISSIONS FULL / y/`` the
    remainder pairs ``/ y/`` as a division and looks balanced (#517). That
    opener is trusted only where an operand is expected (after a symbol
    operator, an opening bracket, ``ASSERT``/``VALUE``/``DEFAULT`` or a word
    operator such as ``CONTAINS``, ``IN`` or ``AND``, as in ``ASSERT $value
    CONTAINS /x PERMISSIONS FULL / y/``, #558): with an operand on its
    left, as in ``$value/2`` or ``1/2``, it is a division written without
    spaces. It is also trusted only when it is the first ``/`` of the
    text before the start, whether an earlier one was read as a division, as
    a stray closer or as part of a consumed pattern, since any such ``/`` can
    belong to a misread pattern: in the server's ``ASSERT ($value CONTAINS /
    a+/) PERMISSIONS FOR ...`` the ``/`` after ``a+`` closes ``/ a+/`` and
    opens nothing, in ``/ a\\/ b/`` the escaped slash brings the division
    count back to zero before the closing ``/``, and in ``($value/2 + /
    a+/)`` the unspaced division is consumed as a pattern up to the ``/``
    before ``a+``. A pattern opening with a space is not trusted
    either, because the scan can misread one (``ASSERT / +/``, below), so
    ``= / x PERMISSIONS FULL / y/`` and ``CONTAINS / x PERMISSIONS FULL /
    y/`` remain a known limit, whatever operator comes before. A body with no
    start left has no clause.

    The scan ending balanced is not enough to rule the candidates out: a
    pattern starting with a space after a keyword (``ASSERT / +/``) is read
    as a division, and the next ``/`` can then open a pattern that hides the
    real clause.

    Args:
        body: The statement body after ``DEFINE FIELD name ON table``

    Returns:
        ``(definition, permissions)``: the body before the clause, and the
        clause after its ``PERMISSIONS`` keyword (empty when there is none)
    """
    index, _, _ = _scan_field_body(body, find_clause=True)
    if index != -1:
        return body[:index], body[index + len("PERMISSIONS") :].strip()

    starts = [m.start() for m in _FIELD_PERMISSIONS_START.finditer(body)]
    for start in reversed(starts):
        opener = _scan_field_body(body[:start], find_clause=False)[2]
        if opener != -1 and not body[opener + 1].isspace() and _operand_expected_at(body, opener):
            continue
        remainder = body[start + len("PERMISSIONS") :]
        if _scan_field_body(remainder, find_clause=False)[1]:
            return body[:start], remainder.strip()
    return body, ""


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
    body, raw_permissions = _split_field_permissions(match.group(3))

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

    # Parse COMMENT clause — single- or double-quoted, backslash-escaped
    raw_comment = clauses.get("COMMENT")
    comment = _unquote_surql_string(raw_comment) if raw_comment else None

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
        comment=comment,
        # Stored normalised: FULL is a field's default, NONE is a restriction.
        permissions=_effective_field_permissions(_parse_permissions(raw_permissions)),
    )


def _parse_default_value(raw: str | None) -> Any:
    """Parse a DEFAULT clause value into a Python object.

    A quoted text becomes a ``str``; an unquoted expression such as
    ``time::now()`` becomes a :class:`~surreal_orm.SurrealFunc`, so the two
    are rendered back differently.
    """
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
    if len(raw) >= 2 and raw[0] in ("'", '"') and raw[-1] == raw[0]:
        return _unquote_surql_string(raw)

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

    # Function call or expression (e.g., time::now()). Carried as a
    # SurrealFunc so it is re-emitted as an expression, while every plain
    # str is rendered back as a quoted text.
    return _default_expression(raw)


def _default_expression(raw: str) -> SurrealFunc:
    """Wrap an unquoted DEFAULT expression the server reported.

    ``SurrealFunc`` refuses a few keyword-and-semicolon shapes meant for
    hand-written values. This text is the server's own definition, already
    accepted by it, so it is carried as-is when that check would refuse it.

    Args:
        raw: The expression, as the server echoes it

    Returns:
        The expression, marked as such
    """
    try:
        return SurrealFunc(raw)
    except ValueError:
        # Written back as ``_surreal_func_from_server(...)``, so a migration
        # file generated from it does not run the refused check on import.
        return _surreal_func_from_server(raw)


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
    relation_in: str | None = None
    relation_out: str | None = None
    enforced = False
    if "TYPE" in clauses:
        raw_type = clauses["TYPE"].strip()
        upper_type = raw_type.upper()
        if upper_type.startswith("RELATION"):
            table_type = "relation"
            # Parse IN/OUT/ENFORCED from the rest of the TYPE clause
            rel_body = raw_type[len("RELATION") :].strip()
            in_match = re.search(r"\bIN\s+(.+?)(?:\s+OUT\b|\s+ENFORCED\b|$)", rel_body, re.IGNORECASE)
            if in_match:
                relation_in = in_match.group(1).strip()
            out_match = re.search(r"\bOUT\s+(.+?)(?:\s+ENFORCED\b|$)", rel_body, re.IGNORECASE)
            if out_match:
                relation_out = out_match.group(1).strip()
            if "ENFORCED" in rel_body.upper():
                enforced = True
        elif upper_type == "ANY":
            table_type = "any"
        else:
            table_type = upper_type.lower()

    # Changefeed
    changefeed = clauses.get("CHANGEFEED") or None
    if changefeed:
        changefeed = changefeed.split()[0]  # Just the duration part

    # Permissions
    permissions = _parse_permissions(clauses.get("PERMISSIONS"))

    # Comment — single- or double-quoted, backslash-escaped
    raw_comment = clauses.get("COMMENT")
    comment = _unquote_surql_string(raw_comment) if raw_comment else None

    # Materialized view (AS SELECT ... or AS (SELECT ...))
    # We re-extract from the raw body because _extract_clauses may split on
    # ``AS`` tokens inside the view query itself (e.g. ``count() AS total``).
    view_query: str | None = None
    as_match = re.search(
        r"\bAS\s+(?:\(\s*)?(SELECT\b.+)",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if as_match:
        vq = as_match.group(1).strip()
        # Strip trailing parenthesis from AS (SELECT ...) form
        if vq.endswith(")"):
            vq = vq[:-1].strip()
        view_query = vq

    return {
        "name": table_name,
        "schema_mode": schema_mode,
        "table_type": table_type,
        "changefeed": changefeed,
        "permissions": permissions,
        "view_query": view_query,
        "relation_in": relation_in,
        "relation_out": relation_out,
        "enforced": enforced,
        "comment": comment,
    }


def _split_permission_groups(raw: str) -> list[str]:
    """Split a PERMISSIONS clause into its ``FOR ...`` groups.

    Scanning rather than matching a regex, so that a ``FOR`` inside a string,
    a regex literal or a parenthesised sub-expression does not start a new
    group (see :func:`_top_level`), and a double-quoted ``"it's"`` does not
    open a string that runs to the end of the clause (#568). The regex this
    replaced also backtracked quadratically on a long run of ``FOR`` tokens.

    Args:
        raw: The clause body, without the ``PERMISSIONS`` keyword

    Returns:
        One string per group, each still starting with ``FOR``
    """
    groups: list[str] = []
    start: int | None = None

    for i in _top_level(raw):
        # A group starts at a `FOR` token: preceded by nothing, a separator or
        # whitespace, and followed by whitespace.
        if (
            raw[i : i + 3].upper() == "FOR"
            and (i == 0 or raw[i - 1] in " \t\n,")
            and i + 3 < len(raw)
            and raw[i + 3] in " \t\n"
        ):
            if start is not None:
                groups.append(raw[start:i])
            start = i

    if start is not None:
        groups.append(raw[start:])

    return groups


def _parse_permissions(raw: str | None) -> dict[str, str]:
    """Parse a PERMISSIONS clause into a dict of action → rule.

    A rule is either a condition expression or one of the keywords ``FULL`` /
    ``NONE``, kept uppercase so the renderer can tell them apart from an
    expression — ``FOR select WHERE FULL`` is not the same statement as
    ``FOR select FULL``: the first evaluates ``FULL`` as a field reference,
    gets ``NONE``, and denies.

    Handles formats like:
    - ``FULL`` → {"select": "FULL", "create": "FULL", ...}
    - ``NONE`` → {"select": "NONE", "create": "NONE", ...}
    - ``FOR select WHERE $auth.id = id, FOR create, update, delete NONE``
    """
    if raw is None or raw == "":
        return {}

    raw = raw.strip()
    if raw.upper() == "FULL":
        return dict.fromkeys(_PERMISSION_ACTIONS, "FULL")
    if raw.upper() == "NONE":
        return dict.fromkeys(_PERMISSION_ACTIONS, "NONE")

    permissions: dict[str, str] = {}
    for group in _split_permission_groups(raw):
        # Drop the leading FOR and the comma that separated this group from the
        # next one — it belongs to the clause, not to the expression.
        body = group[3:].strip().rstrip(",").strip()
        if not body:
            continue

        # Either `<actions> WHERE <condition>` or `<actions> FULL|NONE`.
        split = re.split(r"\s+WHERE\s+", body, maxsplit=1, flags=re.IGNORECASE)
        if len(split) == 2:
            actions_str, rule = split[0], split[1].strip()
        else:
            head, _, last = body.rpartition(" ")
            if last.upper() not in ("FULL", "NONE"):
                continue
            actions_str, rule = head, last.upper()

        for action in re.split(r"[,\s]+", actions_str.strip()):
            action = action.strip().lower()
            if action:
                permissions[action] = rule

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
    flags_str = re.sub(r"""COMMENT\s+(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""", "", flags_str, flags=re.IGNORECASE)

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


def parse_define_event(statement: str) -> EventState:
    """Parse a DEFINE EVENT statement into an EventState.

    Examples::

        parse_define_event(
            "DEFINE EVENT audit_create ON users "
            "WHEN $event = 'CREATE' "
            "THEN (CREATE audit_log SET table = 'users', action = 'create')"
        )

    Args:
        statement: Full DEFINE EVENT statement string.

    Returns:
        Populated EventState instance.
    """
    stmt = statement.strip().rstrip(";").strip()

    match = re.match(
        r"DEFINE\s+EVENT\s+(?:IF\s+NOT\s+EXISTS\s+|OVERWRITE\s+)?(\S+)\s+ON\s+(?:TABLE\s+)?(\S+)\s*(.*)",
        stmt,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"Cannot parse DEFINE EVENT statement: {statement!r}")

    event_name = match.group(1)
    table_name = match.group(2)
    body = match.group(3).strip()

    # Extract WHEN clause — everything between WHEN and THEN
    when_match = re.search(r"\bWHEN\s+(.+?)\s+THEN\b", body, re.IGNORECASE | re.DOTALL)
    when = when_match.group(1).strip() if when_match else ""

    # Extract THEN clause — content inside parentheses after THEN
    then_match = re.search(r"\bTHEN\s*\(", body, re.IGNORECASE)
    then = ""
    if then_match:
        then = _extract_balanced_parens(body, then_match.end() - 1)
    else:
        # THEN without parens (single statement)
        then_alt = re.search(r"\bTHEN\s+(.+?)(?:\s+COMMENT\b|$)", body, re.IGNORECASE | re.DOTALL)
        if then_alt:
            then = then_alt.group(1).strip()

    return EventState(
        name=event_name,
        table=table_name,
        when=when,
        then=then,
    )


__all__ = [
    "parse_define_access",
    "parse_define_analyzer",
    "parse_define_event",
    "parse_define_field",
    "parse_define_index",
    "parse_define_table",
]
