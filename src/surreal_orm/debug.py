"""
Debug and profiling utilities for SurrealDB ORM.

Provides ``QueryLogger``, an async context manager that captures all ORM
queries with timing information for performance profiling and debugging.

Example::

    from surreal_orm.debug import QueryLogger

    async with QueryLogger() as logger:
        users = await User.objects().filter(role="admin").exec()
        orders = await Order.objects().filter(user_id=users[0].id).exec()

    for q in logger.queries:
        print(f"{q.sql} — {q.duration_ms:.1f}ms")

    print(f"Total: {logger.total_queries} queries, {logger.total_ms:.1f}ms")
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Self


_active_logger: ContextVar[QueryLogger | None] = ContextVar("_active_query_logger", default=None)


@dataclass
class QueryLog:
    """A single captured query with timing information."""

    sql: str
    variables: dict[str, Any]
    duration_ms: float
    timestamp: float = field(default_factory=time.time)

    def __repr__(self) -> str:
        return f"QueryLog({self.sql!r}, {self.duration_ms:.1f}ms)"


class QueryLogger:
    """
    Async context manager that captures all ORM queries with timing.

    Uses ``contextvars`` for async-safe activation — only queries
    executed within the ``async with`` block are captured.

    Attributes:
        queries: List of captured ``QueryLog`` entries.

    Example::

        async with QueryLogger() as logger:
            users = await User.objects().all()

        print(logger.total_queries)  # 1
        print(logger.total_ms)      # e.g. 2.3
    """

    def __init__(self) -> None:
        self.queries: list[QueryLog] = []
        self._token: Any = None

    async def __aenter__(self) -> Self:
        self._token = _active_logger.set(self)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._token is not None:
            _active_logger.reset(self._token)
            self._token = None

    @property
    def total_queries(self) -> int:
        """Total number of captured queries."""
        return len(self.queries)

    @property
    def total_ms(self) -> float:
        """Total duration of all captured queries in milliseconds."""
        return sum(q.duration_ms for q in self.queries)

    def _record(self, sql: str, variables: dict[str, Any], duration_ms: float) -> None:
        """Record a query execution."""
        self.queries.append(QueryLog(sql=sql, variables=variables, duration_ms=duration_ms))

    def __repr__(self) -> str:
        return f"QueryLogger({self.total_queries} queries, {self.total_ms:.1f}ms)"


def _log_query(sql: str, variables: dict[str, Any], duration_ms: float) -> None:
    """Log a query to the active QueryLogger, if any."""
    logger = _active_logger.get(None)
    if logger is not None:
        logger._record(sql, variables, duration_ms)


def _start_timer() -> float:
    """Return a high-resolution timer value."""
    return time.perf_counter()


def _elapsed_ms(start: float) -> float:
    """Return elapsed time in milliseconds since *start*."""
    return (time.perf_counter() - start) * 1000.0


__all__ = ["QueryLog", "QueryLogger"]
