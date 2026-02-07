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

from typing import Any


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
            if "__" in key:
                field_name, lookup_name = key.split("__", 1)
            else:
                field_name, lookup_name = key, "exact"
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
