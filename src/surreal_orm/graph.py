"""
Helpers for recursive graph traversal (``reachable_from()``, ``paths_from()``,
``shortest_path()``).

Everything here is pure: argument checks, record-reference resolution and the
small renderers the QuerySet compilers share. Nothing sends a query.

Two server facts shape these helpers:

- **Depths are written into the query text**, not bound: SurrealDB parses the
  recursion range (``{1..3+collect}``) as syntax. They are checked as integers
  before rendering, and capped at 256, the server's fixed recursion limit for
  recursive idioms (not configurable).
- **The source must be a record value.** A bound string is read as a string:
  ``$src.{..}(->edge->t)`` on ``'t:a'`` walks nothing and the result is an
  empty list, with no error. The same holds for an edge's ``in`` / ``out``
  compared with a string, so :func:`coerce_edge_value` turns those values into
  ``RecordId`` objects and refuses a bare id, which has no table to resolve
  against on an edge.

A bounded ``+shortest`` that does not reach its target returns the frontier it
stopped at (an array of arrays), not an empty result; :func:`as_path` tells a
path from anything else.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final, Literal

from surreal_sdk.protocol.cbor import RecordId

from .q import Q
from .utils import (
    _VARIABLE_REFERENCE_RE,
    SAFE_IDENTIFIER_RE,
    is_declared_variable_reference,
    parse_record_id,
    validate_identifier,
)

GraphDirection = Literal["out", "in", "both"]
"""Direction of each traversal step: ``->`` (out), ``<-`` (in) or ``<->`` (both)."""

MAX_GRAPH_DEPTH: Final[int] = 256
"""SurrealDB's fixed recursion limit for recursive idioms (not configurable server-side)."""

MAX_PATH_DEPTH: Final[int] = 32
"""The ORM's cap on ``paths_from()`` ``max_depth``: ``+path`` does not cut cycles, its cost is exponential."""

ARROWS: Final[dict[str, str]] = {"out": "->", "in": "<-", "both": "<->"}
"""The arrow written on each side of the edge table for a direction."""

#: Lookups on an edge's ``in`` / ``out`` that compare whole records.
_RECORD_LOOKUPS: Final[frozenset[str]] = frozenset({"exact", "in", "not_in"})


@dataclass(frozen=True)
class GraphWalk:
    """A validated traversal, as ``reachable_from()`` stores it on a QuerySet.

    Attributes:
        source: The record the walk starts from.
        edge: The edge table, a validated identifier.
        node_table: The QuerySet's table, a validated identifier.
        direction: Step direction.
        min_depth: Lowest depth returned (``None``: from the first step).
        max_depth: Highest depth walked (``None``: up to the server's limit).
        node_filter: Condition on each node reached, or ``None``.
        edge_filter: Condition on each edge crossed, or ``None``.
        include_source: Whether the source itself is part of the result.
    """

    source: RecordId
    edge: str
    node_table: str
    direction: GraphDirection
    min_depth: int
    max_depth: int | None
    node_filter: Q | None
    edge_filter: Q | None
    include_source: bool


def _invalid_ref(value: object, arg: str) -> ValueError:
    return ValueError(
        f"Invalid {arg}: {value!r}. Expected a model instance, a RecordId or a 'table:id' string with an identifier table"
    )


def _check_record_id(rid: RecordId, value: object, arg: str) -> RecordId:
    """Return *rid* if its table is an identifier and its key is not empty (E10)."""
    if not isinstance(rid.table, str) or not SAFE_IDENTIFIER_RE.fullmatch(rid.table) or rid.id in ("", None):
        raise _invalid_ref(value, arg)
    return rid


def to_graph_record(value: object, default_table: str, *, arg: str) -> RecordId:
    """
    Resolve a traversal source or target to a ``RecordId``.

    A ``"table:id"`` string is read by :func:`~surreal_orm.utils.parse_record_id`,
    the reader ``get()`` and the foreign-key coercion use, so a string means the
    same record everywhere in the ORM: split at the first ``:``, a backticked key
    unescaped, any other key taken as written and kept a string
    (``"t:1"`` is the key ``"1"``, not ``1``). A key spelled ``⟨…⟩`` is refused
    rather than read as the literal key ``⟨7x⟩``, which would match nothing.

    Args:
        value: A saved model instance, a ``RecordId``, a ``"table:id"`` string or
            a bare id.
        default_table: The table a bare id belongs to (the QuerySet's table).
        arg: The argument name used in error messages (``"source"``, ``"target"``).

    Returns:
        The record, as a ``RecordId``.

    Raises:
        TypeError: *value* is of another type.
        ValueError: An unsaved instance, a table that is not an identifier, an
            empty key, or a key written ``⟨…⟩``.
    """
    from .model_base import BaseSurrealModel

    if isinstance(value, BaseSurrealModel):
        record_id = value.record_id
        if record_id is None:
            raise ValueError(f"{arg} must be saved before it can be traversed (it has no id)")
        return _check_record_id(record_id, value, arg)

    if isinstance(value, RecordId):
        return _check_record_id(value, value, arg)

    if isinstance(value, str):
        raw_key = value.split(":", 1)[1] if ":" in value else value
        if raw_key.startswith("⟨") or raw_key.endswith("⟩"):
            raise ValueError(
                f"Invalid {arg}: {value!r}. The ⟨…⟩ record-id spelling is not supported; "
                "escape the key with backticks or pass RecordId(table, key)"
            )
        table, key = parse_record_id(value)
        return _check_record_id(RecordId(table=default_table if table is None else table, id=key), value, arg)

    raise TypeError(f"{arg} must be a model instance, a RecordId or a 'table:id' string, got {type(value).__name__}")


def check_include_source(source: RecordId, node_table: str, include_source: bool) -> None:
    """
    Refuse ``include_source=True`` for a source outside the queried table.

    ``+inclusive`` returns the source itself among the results, which are read
    as records of *node_table*.

    Raises:
        ValueError: *include_source* is true and the source's table differs.
    """
    if include_source and source.table != node_table:
        raise ValueError(
            f"include_source=True needs a source in {node_table!r}, got {str(source)!r}: the source would be "
            f"returned as a {node_table!r} record; pass include_source=False"
        )


def edge_table_name(edge: object) -> str:
    """
    Resolve the edge argument to a table name.

    Args:
        edge: An edge table name, or a model class whose table is the edge table.

    Returns:
        The table name, a validated identifier.

    Raises:
        TypeError: *edge* is neither a string nor a model class.
        ValueError: The name is not an identifier.
    """
    from .model_base import BaseSurrealModel

    if isinstance(edge, type) and issubclass(edge, BaseSurrealModel):
        name = edge.get_table_name()
    elif isinstance(edge, str):
        name = edge
    else:
        raise TypeError(f"edge must be a table name or a model class, got {type(edge).__name__}")
    validate_identifier(name, "edge table")
    return name


def check_direction(direction: object, *, allow_both: bool, method: str) -> str:
    """
    Check a traversal direction.

    Args:
        direction: ``"out"``, ``"in"`` or ``"both"``.
        allow_both: Whether *method* accepts ``"both"``.
        method: The calling method's name, for the error message.

    Returns:
        The direction.

    Raises:
        ValueError: An unknown direction, or ``"both"`` where it is not allowed.
    """
    if not isinstance(direction, str) or direction not in ARROWS:
        raise ValueError(f"direction must be 'out', 'in' or 'both', got {direction!r}")
    if direction == "both" and not allow_both:
        raise ValueError(
            f"{method}() does not support direction='both': each '<->' step also returns the record it "
            "starts from, so every path would repeat its own nodes"
        )
    return direction


def coerce_edge_value(field: str, lookup: str, value: Any, declared: Collection[str]) -> Any:
    """
    Convert an ``edge_filter`` value compared with an edge's ``in`` / ``out``.

    On an edge table ``in`` and ``out`` are always records, and a bound string
    never equals a record: ``out = 'users:bob'`` matches nothing, with no error.
    For ``in`` / ``out`` with a lookup comparing whole records (``exact``,
    ``in``, ``not_in``), each value (element-wise for ``in`` / ``not_in``)
    becomes a ``RecordId``: a model instance, a ``RecordId`` or a
    ``"table:id"`` string. A reference to a declared variable is left as is.
    Every other field and lookup is returned unchanged.

    Args:
        field: The edge field filtered on.
        lookup: The lookup name.
        value: The value as supplied.
        declared: Variable names the query is sent with.

    Returns:
        The converted value (a list for ``in`` / ``not_in`` containers).

    Raises:
        ValueError: A value that is not a record after conversion (a bare id,
            a number, ...), or an unsaved model instance.
    """
    if field not in ("in", "out") or lookup not in _RECORD_LOOKUPS:
        return value
    if is_declared_variable_reference(value, declared):
        return value
    if lookup in ("in", "not_in") and isinstance(value, (list, tuple, set)):
        return [_coerce_edge_element(field, item, declared) for item in value]
    return _coerce_edge_element(field, value, declared)


def _coerce_edge_element(field: str, value: Any, declared: Collection[str]) -> Any:
    from .model_base import _to_record_link

    if is_declared_variable_reference(value, declared):
        return value
    result = _to_record_link(value, None)
    if not isinstance(result, RecordId):
        raise ValueError(
            f"edge_filter on {field!r} compares records: pass a model instance, a RecordId or a 'table:id' "
            f"string, got {value!r}"
        )
    return result


def _references(value: Any) -> set[str]:
    """The names of every ``$identifier`` string in *value* (or its elements)."""
    items = value if isinstance(value, (list, tuple, set)) else (value,)
    return {item[1:] for item in items if isinstance(item, str) and _VARIABLE_REFERENCE_RE.fullmatch(item) is not None}


def check_filter(value: object, *, arg: str, edge: bool = False) -> Q | None:
    """
    Check a ``node_filter`` / ``edge_filter`` argument.

    Walks every child of the ``Q``. With *edge*, each leaf goes through
    :func:`coerce_edge_value` so a value that can never match an edge's
    ``in`` / ``out`` fails at the call; a ``$name`` string is let through, since
    ``.variables()`` may declare it later (it is checked again at compile). The
    converted values are discarded: the step is rendered from the original
    ``Q``.

    Args:
        value: A ``Q`` or ``None``.
        arg: The argument name, for error messages.
        edge: Whether *value* filters edges.

    Returns:
        The ``Q``, or ``None`` for ``None`` and a ``Q`` without conditions.

    Raises:
        TypeError: *value* is neither a ``Q`` nor ``None``.
        ValueError: A ``Subquery`` value, or (with *edge*) an ``in`` / ``out``
            value that is not a record.
    """
    if value is None:
        return None
    if not isinstance(value, Q):
        raise TypeError(f"{arg} must be a Q object or None, got {type(value).__name__}")
    if not value.children:
        return None
    _walk_filter(value, edge=edge)
    return value


def _walk_filter(q: Q, *, edge: bool) -> None:
    from .subquery import Subquery

    for child in q.children:
        if isinstance(child, Q):
            _walk_filter(child, edge=edge)
            continue
        field, lookup, value = child
        items = value if isinstance(value, (list, tuple, set)) else (value,)
        if any(isinstance(item, Subquery) for item in items):
            raise ValueError("Subquery values are not supported in node_filter / edge_filter")
        if edge:
            coerce_edge_value(field, lookup, value, _references(value))


def render_range(min_depth: int | None, max_depth: int | None) -> str:
    """Render a recursion range: ``"1..3"``, ``"1.."``, ``"..3"`` or ``".."``."""
    low = "" if min_depth is None else str(min_depth)
    high = "" if max_depth is None else str(max_depth)
    return f"{low}..{high}"


def rid_key(rid: RecordId) -> tuple[str, str]:
    """
    A hashable key for a ``RecordId`` (the dataclass itself is unhashable).

    The key keeps the id's type (``"1"`` and ``1`` differ) and ignores the key
    order of an object id.
    """
    return rid.table, json.dumps(rid.id, sort_keys=True, default=str)


def as_path(raw: object, target: RecordId) -> list[RecordId] | None:
    """
    Read a ``+shortest`` result as a path to *target*.

    A path is a non-empty list of ``RecordId`` ending at *target*. Anything else
    — ``NONE``, an empty list, the frontier a bounded search stopped at (an
    array of arrays), a list ending elsewhere — means no path.

    Returns:
        The path, or ``None``.
    """
    if not isinstance(raw, list) or not raw:
        return None
    if not all(isinstance(item, RecordId) for item in raw):
        return None
    if rid_key(raw[-1]) != rid_key(target):
        return None
    return raw
