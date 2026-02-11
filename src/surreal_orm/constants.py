LOOKUP_OPERATORS = {
    "exact": "=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "in": "IN",
    "not_in": "NOT IN",
    "like": "LIKE",
    "ilike": "ILIKE",
    "contains": "CONTAINS",
    "icontains": "CONTAINS",
    "not_contains": "CONTAINSNOT",
    "containsall": "CONTAINSALL",
    "containsany": "CONTAINSANY",
    # startswith/endswith are handled via string::starts_with() / string::ends_with()
    # in _render_condition — they must NOT appear here as SurrealQL operators.
    "match": "MATCH",
    "regex": "REGEX",
    "iregex": "REGEX",
    "isnull": "IS",
}
