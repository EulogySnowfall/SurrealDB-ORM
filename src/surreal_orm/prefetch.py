"""
Prefetch descriptor for fine-grained control over ``prefetch_related()``.

A ``Prefetch`` object lets you customise which related objects are loaded
and how they are attached to the parent instances.

Example::

    from surreal_orm import Prefetch

    # Default prefetch (equivalent to a plain string)
    users = await User.objects().prefetch_related("posts").exec()

    # With a custom QuerySet filter
    users = await User.objects().prefetch_related(
        Prefetch("posts", queryset=Post.objects().filter(published=True)),
    ).exec()

    # Store results under a different attribute name
    users = await User.objects().prefetch_related(
        Prefetch("posts", queryset=Post.objects().filter(published=True), to_attr="published_posts"),
    ).exec()
    # user.published_posts  ← list of published posts
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .query_set import QuerySet


class Prefetch:
    """
    Describe a relation to prefetch with optional filtering.

    Args:
        relation_name: The edge table or relation field name to traverse.
        queryset: An optional ``QuerySet`` whose **filters** are appended as
            extra AND conditions on the edge table query.  The filters should
            reference fields that exist on the **edge record** (not the target
            model).  If ``None``, all related objects are fetched.
        to_attr: The attribute name on each parent instance where the
            prefetched list is stored.  Defaults to ``relation_name``.

    Note:
        The ``queryset`` filters are applied to the edge table directly
        (``SELECT ... FROM <relation_name> WHERE in IN [...] AND <filters>``).
        To filter by target-model fields, use a raw query or post-filter in
        Python after prefetch.

    Example::

        Prefetch("posts")
        Prefetch("posts", queryset=Post.objects().filter(published=True))
        Prefetch("posts", to_attr="published_posts")
    """

    def __init__(
        self,
        relation_name: str,
        queryset: QuerySet[Any] | None = None,
        to_attr: str | None = None,
    ) -> None:
        self.relation_name = relation_name
        self.queryset = queryset
        self.to_attr = to_attr or relation_name

    def __repr__(self) -> str:
        parts = [repr(self.relation_name)]
        if self.queryset is not None:
            parts.append(f"queryset={self.queryset!r}")
        if self.to_attr != self.relation_name:
            parts.append(f"to_attr={self.to_attr!r}")
        return f"Prefetch({', '.join(parts)})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Prefetch):
            return (
                self.relation_name == other.relation_name and self.queryset is other.queryset and self.to_attr == other.to_attr
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.relation_name, self.to_attr))


__all__ = ["Prefetch"]
