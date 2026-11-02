import re

# Only operators that are valid SurrealQL WHERE-clause operators.
# Lookups that require function calls (like, ilike, startswith, endswith,
# regex, iregex, icontains, isnull, match) are handled as special cases
# in QuerySet._render_condition.
LOOKUP_OPERATORS = {
    "exact": "=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "in": "IN",
    "not_in": "NOT IN",
    "contains": "CONTAINS",
    "not_contains": "CONTAINSNOT",
    "containsall": "CONTAINSALL",
    "containsany": "CONTAINSANY",
}

# Element filter lookups: the value is a Q evaluated against each element of
# an array field (``items__any=Q(qty__gt=2)``). Rendered as ``F[WHERE …]``
# behind a ``type::is_array(F)`` guard in QuerySet._render_condition.
ELEMENT_LOOKUPS = frozenset({"any", "all", "none"})

# All valid lookup names (for error messages / validation)
VALID_LOOKUPS = {
    *ELEMENT_LOOKUPS,
    *LOOKUP_OPERATORS,
    "like",
    "ilike",
    "icontains",
    "startswith",
    "istartswith",
    "endswith",
    "iendswith",
    "regex",
    "iregex",
    "match",
    "isnull",
}

_REGEX_SPECIAL = re.compile(r"([\\.\[{()*+?^$|])")


def like_to_regex(pattern: str) -> str:
    """Convert a SQL LIKE pattern (``%`` and ``_``) to an anchored regex.

    ``%`` → ``.*``  (zero or more chars)
    ``_`` → ``.``   (exactly one char)

    The result is anchored with ``^…$`` so it behaves like SQL LIKE.
    """
    parts: list[str] = []
    for ch in pattern:
        if ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(_REGEX_SPECIAL.sub(r"\\\1", ch))
    return f"^{''.join(parts)}$"
