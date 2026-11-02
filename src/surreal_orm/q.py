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
