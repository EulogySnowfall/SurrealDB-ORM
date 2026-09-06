"""
Shared helpers for building SurrealQL query strings.

Lives in the SDK because both layers need it and the dependency only runs one
way: ``surreal_orm`` imports from ``surreal_sdk``, never the reverse.
"""

import re
from collections.abc import Mapping

__all__ = ["substitute_params"]

#: A ``$name`` reference. ``\w+`` is greedy on purpose: it captures the whole
#: identifier, so ``$state`` cannot match the prefix of ``$state_backup``.
_PARAM_REFERENCE = re.compile(r"\$(\w+)")


def substitute_params(query: str, replacements: Mapping[str, str]) -> str:
    """
    Replace ``$name`` references with pre-rendered text, in a single pass.

    Two properties matter, and both are easy to lose by hand:

    **The text is inserted verbatim.** ``re.sub`` reads a replacement *string*
    as a mini-pattern — ``\\1`` is a backreference, ``\\g<0>`` a group
    reference, and an unknown escape is an error. Rendered SurrealQL is full of
    backslashes (JSON doubles every literal one; ``_format_value`` escapes
    quotes), so passing it as a string raises on some inputs and silently
    collapses backslashes on others. A *callable* replacement is exempt from
    that parsing.

    **It is one pass over the original query.** Substituting one key at a time
    re-scans text that was just inserted, so a ``$b`` occurring inside a string
    value of ``$a`` gets replaced too — and which one wins depends on mapping
    order.

    A reference with no entry in *replacements* is left as it was, so callers
    can inline some parameters and leave the rest as bindings.

    Args:
        query: SurrealQL containing ``$name`` references
        replacements: Reference name to the exact text that replaces it

    Returns:
        The query with every known reference substituted

    Examples:
        substitute_params("SET a = $a", {"a": '{"x": 1}'})   # 'SET a = {"x": 1}'
        substitute_params("SET a = $a", {})                  # 'SET a = $a'
    """
    if not replacements:
        return query

    return _PARAM_REFERENCE.sub(lambda match: replacements.get(match.group(1), match.group(0)), query)
