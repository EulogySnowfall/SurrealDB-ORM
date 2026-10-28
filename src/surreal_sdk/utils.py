"""
Shared helpers for building SurrealQL query strings.

Lives in the SDK because both layers need it and the dependency only runs one
way: ``surreal_orm`` imports from ``surreal_sdk``, never the reverse.
"""

import re
from collections.abc import Mapping

__all__ = ["find_param_references", "substitute_params", "unescape_record_id"]

#: A ``$name`` reference. ``\w+`` is greedy on purpose: it captures the whole
#: identifier, so ``$state`` cannot match the prefix of ``$state_backup``.
#: ``re.ASCII`` matches SurrealDB's own identifier lexer, which is ASCII — an
#: accented name is not a reference the server would bind either.
_PARAM_REFERENCE = re.compile(r"\$(\w+)", re.ASCII)


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
    r"""Read back a record ID written by ``surreal_orm.utils.escape_record_id``.

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
