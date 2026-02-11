"""
Tests for the debug module — QueryLog and QueryLogger.
"""

from __future__ import annotations

from surreal_orm.debug import QueryLog, QueryLogger, _log_query, _active_logger


# ---------------------------------------------------------------------------
# QueryLog
# ---------------------------------------------------------------------------


class TestQueryLog:
    """Tests for QueryLog dataclass."""

    def test_basic_fields(self) -> None:
        log = QueryLog(sql="SELECT * FROM users;", variables={}, duration_ms=1.5)
        assert log.sql == "SELECT * FROM users;"
        assert log.variables == {}
        assert log.duration_ms == 1.5
        assert log.timestamp > 0

    def test_with_variables(self) -> None:
        log = QueryLog(
            sql="SELECT * FROM users WHERE age > $_f0;",
            variables={"_f0": 18},
            duration_ms=2.3,
        )
        assert log.variables == {"_f0": 18}

    def test_repr(self) -> None:
        log = QueryLog(sql="SELECT 1;", variables={}, duration_ms=0.5)
        assert "SELECT 1;" in repr(log)
        assert "0.5ms" in repr(log)


# ---------------------------------------------------------------------------
# QueryLogger
# ---------------------------------------------------------------------------


class TestQueryLogger:
    """Tests for QueryLogger context manager."""

    def test_initial_state(self) -> None:
        logger = QueryLogger()
        assert logger.queries == []
        assert logger.total_queries == 0
        assert logger.total_ms == 0.0

    async def test_context_manager_activates(self) -> None:
        assert _active_logger.get(None) is None
        async with QueryLogger() as logger:
            assert _active_logger.get(None) is logger
        assert _active_logger.get(None) is None

    async def test_record_query(self) -> None:
        logger = QueryLogger()
        logger._record("SELECT 1;", {}, 1.5)
        logger._record("SELECT 2;", {"x": 1}, 2.0)
        assert logger.total_queries == 2
        assert logger.total_ms == 3.5
        assert logger.queries[0].sql == "SELECT 1;"
        assert logger.queries[1].variables == {"x": 1}

    async def test_log_query_function(self) -> None:
        """_log_query records when logger is active."""
        async with QueryLogger() as logger:
            _log_query("SELECT * FROM test;", {"a": 1}, 5.0)
        assert logger.total_queries == 1
        assert logger.queries[0].sql == "SELECT * FROM test;"

    async def test_log_query_no_logger(self) -> None:
        """_log_query is a no-op when no logger is active."""
        _log_query("SELECT 1;", {}, 1.0)
        # Should not raise

    async def test_repr(self) -> None:
        async with QueryLogger() as logger:
            _log_query("Q1", {}, 1.0)
            _log_query("Q2", {}, 2.0)
        assert "2 queries" in repr(logger)
        assert "3.0ms" in repr(logger)

    async def test_nested_loggers(self) -> None:
        """Inner logger captures its own queries; outer resumes after."""
        async with QueryLogger() as outer:
            _log_query("outer1", {}, 1.0)
            async with QueryLogger() as inner:
                _log_query("inner1", {}, 2.0)
            _log_query("outer2", {}, 3.0)
        assert outer.total_queries == 2
        assert inner.total_queries == 1
        assert outer.queries[0].sql == "outer1"
        assert outer.queries[1].sql == "outer2"
        assert inner.queries[0].sql == "inner1"
