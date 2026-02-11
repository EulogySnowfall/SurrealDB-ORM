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


class SearchScore:
    """
    BM25 relevance score annotation.

    Wraps ``search::score(ref)`` where ``ref`` is the match-reference
    index (``@0@``, ``@1@``, etc.) used in the search clause.

    Args:
        ref: Match reference index (default 0).

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

    def to_surql(self, alias: str) -> str:
        """Render as ``search::score(N) AS alias``."""
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

    def to_surql(self, alias: str) -> str:
        """Render as ``search::highlight('open', 'close', N) AS alias``."""
        safe_open = self.open_tag.replace("'", "\\'")
        safe_close = self.close_tag.replace("'", "\\'")
        return f"search::highlight('{safe_open}', '{safe_close}', {self.ref}) AS {alias}"

    def __repr__(self) -> str:
        return f"SearchHighlight({self.open_tag!r}, {self.close_tag!r}, {self.ref})"

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SearchHighlight):
            return self.open_tag == other.open_tag and self.close_tag == other.close_tag and self.ref == other.ref
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("SearchHighlight", self.open_tag, self.close_tag, self.ref))


__all__ = ["SearchScore", "SearchHighlight"]
