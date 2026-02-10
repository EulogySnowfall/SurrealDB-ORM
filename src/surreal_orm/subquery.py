"""
Subquery class for embedding a QuerySet as a filter value in another QuerySet.

Subqueries are compiled to inline sub-SELECT expressions with parameterized
variables that are remapped to avoid collisions with the outer query.

Example::

    from surreal_orm import Subquery

    # Users whose age matches any active user's age
    active_ages = User.objects().filter(is_active=True).select("age")
    users = await User.objects().filter(
        age__in=Subquery(active_ages),
    ).exec()
    # Generates: SELECT * FROM users WHERE age IN (SELECT age FROM users WHERE is_active = $_f0);
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .query_set import QuerySet


class Subquery:
    """
    Wrap a ``QuerySet`` for use as a filter value in another ``QuerySet``.

    The inner QuerySet is compiled into an inline sub-SELECT expression
    with parameterized variables.  The shared ``counter`` ensures that
    ``$_fN`` variable names never collide with the outer query.

    Args:
        queryset: The inner ``QuerySet`` to embed as a sub-SELECT.

    Example::

        from surreal_orm import Subquery

        # Filter by subquery result
        top_ids = Order.objects().filter(total__gte=1000).select("user_id")
        users = await User.objects().filter(id__in=Subquery(top_ids)).exec()

        # With ORDER BY and LIMIT
        recent = User.objects().order_by("-created_at").select("id").limit(10)
        posts = await Post.objects().filter(author_id__in=Subquery(recent)).exec()
    """

    def __init__(self, queryset: QuerySet) -> None:
        self.queryset = queryset

    def to_surql(
        self,
        variables: dict[str, Any],
        counter: list[int],
    ) -> str:
        """
        Compile the inner QuerySet to a parenthesized sub-SELECT.

        Uses the shared ``counter`` to generate unique ``$_fN`` variable
        names that don't collide with the outer query.  Inner variable
        bindings are added to the shared ``variables`` dict.

        Args:
            variables: Mutable dict to collect parameterized variables.
            counter: Mutable single-element list ``[int]`` used as
                auto-increment counter.

        Returns:
            The sub-SELECT expression wrapped in parentheses.
        """
        qs = self.queryset

        # SELECT clause — use VALUE for single-field selects so
        # the result is a flat array (required for IN subqueries).
        if qs.select_item:
            if len(qs.select_item) == 1:
                select_clause = f"VALUE {qs.select_item[0]}"
            else:
                select_clause = ", ".join(qs.select_item)
        else:
            select_clause = "*"

        query = f"SELECT {select_clause} FROM {qs._model_table}"

        # WHERE clause — build parts using the shared counter
        parts: list[str] = []
        qs_class = type(qs)

        for field_name, lookup_name, value in qs._filters:
            parts.append(qs_class._render_condition(field_name, lookup_name, value, variables, counter))

        for q in qs._q_filters:
            rendered = qs._render_q(q, variables, counter)
            if rendered:
                parts.append(rendered)

        if parts:
            query += " WHERE " + " AND ".join(parts)

        # ORDER BY
        if qs._order_by:
            query += f" ORDER BY {qs._order_by}"

        # LIMIT
        if qs._limit is not None:
            query += f" LIMIT {qs._limit}"

        # OFFSET
        if qs._offset is not None:
            query += f" START {qs._offset}"

        return f"({query})"

    def __repr__(self) -> str:
        return f"Subquery({self.queryset!r})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Subquery):
            return self.queryset is other.queryset
        return NotImplemented

    def __hash__(self) -> int:
        return id(self.queryset)


__all__ = ["Subquery"]
