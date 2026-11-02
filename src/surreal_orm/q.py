"""
Q objects for building complex query expressions with AND, OR, and NOT logic.

Usage:
    from surreal_orm import Q

    # OR query
    users = await User.objects().filter(
        Q(name__contains="alice") | Q(email__contains="alice"),
    ).exec()

    # AND with OR
    users = await User.objects().filter(
        Q(role="admin") & (Q(age__gte=18) | Q(is_verified=True)),
    ).exec()

    # NOT
    users = await User.objects().filter(~Q(status="banned")).exec()
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from .constants import ELEMENT_LOOKUPS
from .subquery import Subquery
from .utils import parse_filter_key


class Q:
    """
    Django-style Q object for composing complex query conditions.

    Q objects can be combined using ``|`` (OR), ``&`` (AND), and ``~`` (NOT)
    operators to build expression trees that are compiled into SurrealQL WHERE clauses.

    Each Q object holds either:
    - Leaf conditions parsed from keyword arguments (e.g., ``Q(age__gte=18)``)
    - Child Q objects combined via a connector (AND/OR)
    """

    AND = "AND"
    OR = "OR"

    def __init__(self, **kwargs: Any) -> None:
        self.children: list[Q | tuple[str, str, Any]] = []
        self.connector: str = self.AND
        self.negated: bool = False

        for key, value in kwargs.items():
            # Raises ValueError for a key whose field part is not a field name
            # (or dotted path) or whose lookup is unknown.
            field_name, lookup_name = parse_filter_key(key)
            # An element lookup's value is checked now, not at the first
            # compile, so the error points at the line that built it.
            check_element_lookup(field_name, lookup_name, value)
            self.children.append((field_name, lookup_name, value))

    def __or__(self, other: Q) -> Q:
        """Combine two Q objects with OR logic."""
        result = Q()
        result.connector = self.OR
        result.children = [self, other]
        return result

    def __and__(self, other: Q) -> Q:
        """Combine two Q objects with AND logic."""
        result = Q()
        result.connector = self.AND
        result.children = [self, other]
        return result

    def __invert__(self) -> Q:
        """Negate a Q object."""
        result = Q()
        result.connector = self.AND
        result.children = [self]
        result.negated = True
        return result

    def __repr__(self) -> str:
        if self.negated:
            return f"~Q({self.children!r})"
        if self.connector == self.OR:
            return f"Q.OR({self.children!r})"
        return f"Q({self.children!r})"


def iter_q_leaves(q: Q) -> Iterator[tuple[str, str, Any]]:
    """Yield every leaf condition ``(field, lookup, value)`` of ``q``, at any depth.

    Walks ``children`` depth first, in order. The values themselves are not
    entered: a ``Q`` used as a lookup value is a leaf's value, not a child.
    """
    for child in q.children:
        if isinstance(child, Q):
            yield from iter_q_leaves(child)
        else:
            yield child


def q_has_condition(q: Q) -> bool:
    """Return ``True`` when ``q`` holds a leaf condition at some depth.

    This is the same as "``q`` renders to a non-empty text": a leaf always
    renders, while ``Q()``, ``Q() | Q()``, ``Q() & Q()`` and ``~Q()`` render to
    nothing. ``Q(a=1) | Q()`` has a condition.
    """
    return next(iter_q_leaves(q), None) is not None


def check_element_lookup(field: str, lookup: str, value: Any) -> None:
    """Check the value of an element lookup (``__any`` / ``__all`` / ``__none``).

    A no-op for every other lookup. Called when a ``Q`` leaf is built, by
    ``QuerySet.filter()`` for each keyword, and again by the renderer (a
    ``Q``'s ``children`` can be set by hand), so the error is the same
    whichever path reaches it.

    Nested element lookups inside ``value`` were checked when their own ``Q``
    was built; the renderer checks hand-built ones.

    Args:
        field: The field part of the filter key.
        lookup: The lookup name.
        value: The filter value.

    Raises:
        TypeError: If ``value`` is not a ``Q``, or holds a ``Subquery`` value
            (an element condition is evaluated per array element).
        ValueError: If ``value`` holds no condition at any depth (``Q()``,
            ``Q() | Q()``, ``Q() & Q()``, ``~Q()`` render to nothing).
    """
    if lookup not in ELEMENT_LOOKUPS:
        return
    if not isinstance(value, Q):
        raise TypeError(
            f"Value for lookup '{lookup}' on field '{field}' must be a Q object (the condition each element is tested "
            f"against), got {type(value).__name__!r}."
        )
    if not q_has_condition(value):
        raise ValueError(f"Lookup '{lookup}' on {field!r} needs a non-empty Q() element condition.")
    for leaf_field, leaf_lookup, leaf_value in iter_q_leaves(value):
        if isinstance(leaf_value, Subquery):
            raise TypeError(subquery_in_element_condition_message(leaf_field, leaf_lookup))


def subquery_in_element_condition_message(field: str, lookup: str) -> str:
    """The text of the error for a ``Subquery`` value inside an element condition."""
    return (
        f"A Subquery cannot be used inside an element condition ({f'{field}__{lookup}'!r}): "
        "element conditions are evaluated per array element."
    )
