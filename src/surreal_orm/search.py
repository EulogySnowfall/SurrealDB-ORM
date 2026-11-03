"""
Full-text search annotation helpers for SurrealDB.

Provides ``SearchScore`` and ``SearchHighlight`` classes that can be used
with ``QuerySet.annotate()`` to compute BM25 relevance scores and
hit highlighting in full-text search queries.

Example::

    from surreal_orm import SearchScore, SearchHighlight

    results = await Post.objects().search(title="quantum").annotate(
        relevance=SearchScore(0),
        snippet=SearchHighlight("<b>", "</b>", 0),
    ).exec()
"""

from __future__ import annotations

from typing import Any

from .utils import require_int, surql_string_literal, validate_identifier


class SearchScore:
    """
    BM25 relevance score annotation.

    Wraps ``search::score(ref)`` where ``ref`` is the match-reference
    index (``@0@``, ``@1@``, etc.) used in the search clause.

    Args:
        ref: Match reference index (default 0).

    Raises:
        TypeError: If ``ref`` is not an ``int`` (``bool`` refused).
        ValueError: If ``ref`` is negative.

    Example::

        # Single-field search
        results = await Post.objects().search(title="quantum").annotate(
            relevance=SearchScore(0),
        ).exec()
        # SELECT *, search::score(0) AS relevance FROM posts
        #   WHERE title @0@ $_s0;
    """

    def __init__(self, ref: int = 0) -> None:
        self.ref = ref

    @property
    def ref(self) -> int:
        """The match reference index."""
        return self._ref

    @ref.setter
    def ref(self, value: int) -> None:
        # Written into the query text by to_surql(), not bound, so it is
        # checked at construction and again whenever it is assigned.
        self._ref = require_int("ref", value, minimum=0)

    def to_surql(self, alias: str) -> str:
        """Render as ``search::score(N) AS alias``.

        Raises:
            ValueError: If ``alias`` is not an identifier.
        """
        validate_identifier(alias, "annotation alias")
        return f"search::score({self.ref}) AS {alias}"

    def __repr__(self) -> str:
        return f"SearchScore({self.ref})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SearchScore):
            return self.ref == other.ref
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("SearchScore", self.ref))


class SearchHighlight:
    """
    Full-text search hit highlighting annotation.

    Wraps ``search::highlight(open, close, ref)`` where ``ref`` is
    the match-reference index.

    Args:
        open_tag: Opening tag for highlights (e.g., ``"<b>"``).
        close_tag: Closing tag for highlights (e.g., ``"</b>"``).
        ref: Match reference index (default 0).

    Raises:
        TypeError: If ``ref`` is not an ``int`` (``bool`` refused).
        ValueError: If ``ref`` is negative.

    Example::

        results = await Post.objects().search(title="quantum").annotate(
            snippet=SearchHighlight("<b>", "</b>", 0),
        ).exec()
        # SELECT *, search::highlight('<b>', '</b>', 0) AS snippet FROM posts
        #   WHERE title @0@ $_s0;
    """

    def __init__(self, open_tag: str = "<b>", close_tag: str = "</b>", ref: int = 0) -> None:
        self.open_tag = open_tag
        self.close_tag = close_tag
        self.ref = ref

    @property
    def ref(self) -> int:
        """The match reference index."""
        return self._ref

    @ref.setter
    def ref(self, value: int) -> None:
        # Written into the query text by to_surql(), not bound, so it is
        # checked at construction and again whenever it is assigned.
        self._ref = require_int("ref", value, minimum=0)

    def to_surql(self, alias: str) -> str:
        """Render as ``search::highlight('open', 'close', N) AS alias``.

        Both tags are rendered by
        :func:`~surreal_orm.utils.surql_string_literal`, which escapes the
        backslash as well as the apostrophe, so each tag reaches
        ``search::highlight()`` as written. ``ref`` was checked and
        converted to a plain ``int`` when it was assigned.

        Raises:
            ValueError: If ``alias`` is not an identifier.
        """
        validate_identifier(alias, "annotation alias")
        open_tag = surql_string_literal(self.open_tag)
        close_tag = surql_string_literal(self.close_tag)
        return f"search::highlight({open_tag}, {close_tag}, {self.ref}) AS {alias}"

    def __repr__(self) -> str:
        return f"SearchHighlight({self.open_tag!r}, {self.close_tag!r}, {self.ref})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SearchHighlight):
            return self.open_tag == other.open_tag and self.close_tag == other.close_tag and self.ref == other.ref
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("SearchHighlight", self.open_tag, self.close_tag, self.ref))


__all__ = ["SearchHighlight", "SearchScore"]
