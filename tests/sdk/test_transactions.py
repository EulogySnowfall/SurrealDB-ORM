"""Tests for transaction module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.surreal_sdk.transaction import (
    HTTPTransaction,
    WebSocketTransaction,
    TransactionStatement,
)
from src.surreal_sdk.exceptions import TransactionError
from src.surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus


class TestTransactionStatement:
    """Tests for TransactionStatement dataclass."""

    def test_create_statement(self) -> None:
        """Test creating a statement."""
        stmt = TransactionStatement(
            sql="UPDATE users:1 SET active = $active",
            vars={"active": True},
        )
        assert stmt.sql == "UPDATE users:1 SET active = $active"
        assert stmt.vars == {"active": True}

    def test_create_statement_no_vars(self) -> None:
        """Test creating a statement without variables."""
        stmt = TransactionStatement(sql="DELETE users:1")
        assert stmt.sql == "DELETE users:1"
        assert stmt.vars == {}


class TestHTTPTransaction:
    """Tests for HTTPTransaction class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock HTTP connection."""
        conn = MagicMock()
        conn.query = AsyncMock(
            return_value=QueryResponse(
                results=[
                    QueryResult(status=ResponseStatus.OK, time="1ms", result=None),
                ],
                raw=[],
            )
        )
        return conn

    def test_transaction_initial_state(self, mock_connection: MagicMock) -> None:
        """Test initial transaction state."""
        tx = HTTPTransaction(mock_connection)

        assert not tx.is_active
        assert not tx.is_committed
        assert not tx.is_rolled_back

    @pytest.mark.asyncio
    async def test_transaction_begin(self, mock_connection: MagicMock) -> None:
        """Test transaction begin."""
        tx = HTTPTransaction(mock_connection)

        await tx._begin()

        assert tx.is_active
        assert not tx.is_committed
        assert not tx.is_rolled_back

    @pytest.mark.asyncio
    async def test_transaction_double_begin_raises(self, mock_connection: MagicMock) -> None:
        """Test that double begin raises error."""
        tx = HTTPTransaction(mock_connection)
        await tx._begin()

        with pytest.raises(TransactionError, match="already active"):
            await tx._begin()

    @pytest.mark.asyncio
    async def test_transaction_commit_empty(self, mock_connection: MagicMock) -> None:
        """Test committing empty transaction."""
        tx = HTTPTransaction(mock_connection)
        await tx._begin()

        await tx.commit()

        assert tx.is_committed
        assert not tx.is_active
        mock_connection.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_transaction_commit_with_statements(self, mock_connection: MagicMock) -> None:
        """Test committing transaction with statements."""
        tx = HTTPTransaction(mock_connection)
        await tx._begin()

        await tx.query("UPDATE users:1 SET name = $name", {"name": "Alice"})
        await tx.query("UPDATE users:2 SET name = $name", {"name": "Bob"})

        await tx.commit()

        assert tx.is_committed
        mock_connection.query.assert_called_once()

        # Check the SQL contains BEGIN/COMMIT
        call_args = mock_connection.query.call_args
        sql = call_args[0][0]
        assert "BEGIN TRANSACTION" in sql
        assert "COMMIT TRANSACTION" in sql

    @pytest.mark.asyncio
    async def test_transaction_commit_not_active_raises(self, mock_connection: MagicMock) -> None:
        """Test that commit on inactive transaction raises error."""
        tx = HTTPTransaction(mock_connection)

        with pytest.raises(TransactionError, match="not active"):
            await tx.commit()

    @pytest.mark.asyncio
    async def test_transaction_rollback(self, mock_connection: MagicMock) -> None:
        """Test transaction rollback."""
        tx = HTTPTransaction(mock_connection)
        await tx._begin()
        await tx.query("UPDATE users:1 SET name = 'Alice'")

        await tx.rollback()

        assert tx.is_rolled_back
        assert not tx.is_active
        # HTTP rollback doesn't call server
        mock_connection.query.assert_not_called()

    @pytest.mark.asyncio
    async def test_context_manager_commit(self, mock_connection: MagicMock) -> None:
        """Test context manager auto-commit."""
        async with HTTPTransaction(mock_connection) as tx:
            await tx.query("UPDATE users:1 SET name = 'Alice'")

        assert tx.is_committed

    @pytest.mark.asyncio
    async def test_context_manager_rollback_on_exception(self, mock_connection: MagicMock) -> None:
        """Test context manager auto-rollback on exception."""
        with pytest.raises(ValueError):
            async with HTTPTransaction(mock_connection) as tx:
                await tx.query("UPDATE users:1 SET name = 'Alice'")
                raise ValueError("Test error")

        assert tx.is_rolled_back

    @pytest.mark.asyncio
    async def test_transaction_create(self, mock_connection: MagicMock) -> None:
        """Test create within transaction."""
        tx = HTTPTransaction(mock_connection)
        await tx._begin()

        await tx.create("users", {"name": "Alice", "age": 30})
        await tx.commit()

        call_args = mock_connection.query.call_args
        sql = call_args[0][0]
        assert "CREATE users" in sql

    @pytest.mark.asyncio
    async def test_transaction_update(self, mock_connection: MagicMock) -> None:
        """Test update within transaction."""
        tx = HTTPTransaction(mock_connection)
        await tx._begin()

        await tx.update("users:1", {"name": "Bob"})
        await tx.commit()

        call_args = mock_connection.query.call_args
        sql = call_args[0][0]
        assert "UPDATE users:1" in sql

    @pytest.mark.asyncio
    async def test_transaction_delete(self, mock_connection: MagicMock) -> None:
        """Test delete within transaction."""
        tx = HTTPTransaction(mock_connection)
        await tx._begin()

        await tx.delete("users:1")
        await tx.commit()

        call_args = mock_connection.query.call_args
        sql = call_args[0][0]
        assert "DELETE users:1" in sql

    @pytest.mark.asyncio
    async def test_transaction_relate(self, mock_connection: MagicMock) -> None:
        """Test relate within transaction."""
        tx = HTTPTransaction(mock_connection)
        await tx._begin()

        await tx.relate("users:1", "follows", "users:2")
        await tx.commit()

        call_args = mock_connection.query.call_args
        sql = call_args[0][0]
        assert "RELATE users:1->follows->users:2" in sql


class TestWebSocketTransaction:
    """Tests for WebSocketTransaction class."""

    @pytest.fixture
    def mock_connection(self) -> MagicMock:
        """Create a mock WebSocket connection."""
        conn = MagicMock()
        conn.query = AsyncMock(
            return_value=QueryResponse(
                results=[QueryResult(status=ResponseStatus.OK, time="1ms", result=None)],
                raw=[],
            )
        )
        conn.create = AsyncMock()
        conn.insert = AsyncMock()
        conn.update = AsyncMock()
        conn.merge = AsyncMock()
        conn.delete = AsyncMock()
        conn.relate = AsyncMock()
        return conn

    @pytest.mark.asyncio
    async def test_transaction_begin_sends_command(self, mock_connection: MagicMock) -> None:
        """Test that begin sends BEGIN TRANSACTION."""
        tx = WebSocketTransaction(mock_connection)

        await tx._begin()

        mock_connection.query.assert_called_once_with("BEGIN TRANSACTION;")
        assert tx.is_active

    @pytest.mark.asyncio
    async def test_transaction_commit_sends_command(self, mock_connection: MagicMock) -> None:
        """Test that commit sends COMMIT TRANSACTION."""
        tx = WebSocketTransaction(mock_connection)
        await tx._begin()
        mock_connection.query.reset_mock()

        await tx.commit()

        mock_connection.query.assert_called_once_with("COMMIT TRANSACTION;")
        assert tx.is_committed

    @pytest.mark.asyncio
    async def test_transaction_rollback_sends_command(self, mock_connection: MagicMock) -> None:
        """Test that rollback sends CANCEL TRANSACTION."""
        tx = WebSocketTransaction(mock_connection)
        await tx._begin()
        mock_connection.query.reset_mock()

        await tx.rollback()

        mock_connection.query.assert_called_once_with("CANCEL TRANSACTION;")
        assert tx.is_rolled_back

    @pytest.mark.asyncio
    async def test_transaction_query_executes_immediately(self, mock_connection: MagicMock) -> None:
        """Test that queries execute immediately."""
        tx = WebSocketTransaction(mock_connection)
        await tx._begin()
        mock_connection.query.reset_mock()

        await tx.query("SELECT * FROM users")

        mock_connection.query.assert_called_once_with("SELECT * FROM users", None)

    @pytest.mark.asyncio
    async def test_transaction_create_executes_immediately(self, mock_connection: MagicMock) -> None:
        """Test that create executes immediately."""
        tx = WebSocketTransaction(mock_connection)
        await tx._begin()

        await tx.create("users", {"name": "Alice"})

        mock_connection.create.assert_called_once_with("users", {"name": "Alice"})

    @pytest.mark.asyncio
    async def test_transaction_update_executes_immediately(self, mock_connection: MagicMock) -> None:
        """Test that update executes immediately."""
        tx = WebSocketTransaction(mock_connection)
        await tx._begin()

        await tx.update("users:1", {"name": "Bob"})

        mock_connection.update.assert_called_once_with("users:1", {"name": "Bob"})

    @pytest.mark.asyncio
    async def test_transaction_not_active_raises(self, mock_connection: MagicMock) -> None:
        """Test operations on inactive transaction raise error."""
        tx = WebSocketTransaction(mock_connection)

        with pytest.raises(TransactionError, match="not active"):
            await tx.query("SELECT * FROM users")

    @pytest.mark.asyncio
    async def test_context_manager_full_flow(self, mock_connection: MagicMock) -> None:
        """Test full context manager flow."""
        async with WebSocketTransaction(mock_connection) as tx:
            await tx.create("users", {"name": "Alice"})

        # Should have called BEGIN, create, COMMIT
        assert mock_connection.query.call_count == 2  # BEGIN + COMMIT
        mock_connection.create.assert_called_once()


class TestTransactionError:
    """Tests for TransactionError exception."""

    def test_error_with_message(self) -> None:
        """Test error with just message."""
        err = TransactionError("Test error")
        assert str(err) == "Test error"
        assert err.code is None
        assert err.rollback_succeeded is None

    def test_error_with_code(self) -> None:
        """Test error with code."""
        err = TransactionError("Test error", code=500)
        assert err.code == 500

    def test_error_with_rollback_status(self) -> None:
        """Test error with rollback status."""
        err = TransactionError("Commit failed", rollback_succeeded=True)
        assert err.rollback_succeeded is True


# Integration tests requiring a running SurrealDB instance


class TestHTTPTransactionIntegration:
    """Integration tests for HTTP transactions with real SurrealDB."""

    @pytest.fixture
    async def connection(self):
        """Create a connected HTTP connection."""
        from src.surreal_sdk.connection.http import HTTPConnection

        conn = HTTPConnection("http://localhost:8001", "test", "test")
        await conn.connect()
        await conn.signin("root", "root")
        yield conn
        await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_transaction_commit_creates_records(self, connection) -> None:
        """Test that committed transaction creates records."""
        # Clean up first
        await connection.query("DELETE tx_test")

        async with connection.transaction() as tx:
            await tx.create("tx_test:1", {"name": "Alice", "value": 1})
            await tx.create("tx_test:2", {"name": "Bob", "value": 2})

        # Verify records exist
        result = await connection.query("SELECT * FROM tx_test ORDER BY value")
        assert result.is_ok
        records = result.all_records
        assert len(records) == 2
        assert records[0]["name"] == "Alice"
        assert records[1]["name"] == "Bob"

        # Clean up
        await connection.query("DELETE tx_test")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_transaction_rollback_no_records(self, connection) -> None:
        """Test that rolled back transaction creates no records."""
        # Clean up first
        await connection.query("DELETE tx_rollback_test")

        try:
            async with connection.transaction() as tx:
                await tx.create("tx_rollback_test:1", {"name": "Alice"})
                raise ValueError("Force rollback")
        except ValueError:
            pass

        # Verify no records exist
        result = await connection.query("SELECT * FROM tx_rollback_test")
        assert result.is_ok
        assert len(result.all_records) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_transaction_update_multiple_records(self, connection) -> None:
        """Test transaction with multiple updates."""
        # Setup test data
        await connection.query("DELETE tx_update_test")
        await connection.query("""
            CREATE tx_update_test:1 SET name = 'Alice', active = false;
            CREATE tx_update_test:2 SET name = 'Bob', active = false;
        """)

        async with connection.transaction() as tx:
            await tx.update("tx_update_test:1", {"name": "Alice", "active": True})
            await tx.update("tx_update_test:2", {"name": "Bob", "active": True})

        # Verify all records updated
        result = await connection.query("SELECT * FROM tx_update_test WHERE active = true")
        assert result.is_ok
        assert len(result.all_records) == 2

        # Clean up
        await connection.query("DELETE tx_update_test")


class TestFunctionIntegrationWithSurrealDB:
    """Integration tests for function calls with real SurrealDB."""

    @pytest.fixture
    async def connection(self):
        """Create a connected HTTP connection."""
        from src.surreal_sdk.connection.http import HTTPConnection

        conn = HTTPConnection("http://localhost:8001", "test", "test")
        await conn.connect()
        await conn.signin("root", "root")
        yield conn
        await conn.close()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_math_sqrt(self, connection) -> None:
        """Test math::sqrt function."""
        result = await connection.fn.math.sqrt(16)
        assert result == 4.0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_math_pow(self, connection) -> None:
        """Test math::pow function."""
        result = await connection.fn.math.pow(2, 8)
        assert result == 256.0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_math_abs(self, connection) -> None:
        """Test math::abs function."""
        result = await connection.fn.math.abs(-42)
        assert result == 42

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_time_now(self, connection) -> None:
        """Test time::now function."""
        result = await connection.fn.time.now()
        assert result is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_string_len(self, connection) -> None:
        """Test string::len function."""
        result = await connection.fn.string.len("hello")
        assert result == 5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_string_lowercase(self, connection) -> None:
        """Test string::lowercase function."""
        result = await connection.fn.string.lowercase("HELLO")
        assert result == "hello"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_array_len(self, connection) -> None:
        """Test array::len function."""
        result = await connection.fn.array.len([1, 2, 3, 4, 5])
        assert result == 5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_crypto_sha256(self, connection) -> None:
        """Test crypto::sha256 function."""
        result = await connection.fn.crypto.sha256("hello")
        assert result is not None
        assert isinstance(result, str)
        assert len(result) == 64  # SHA256 produces 64 hex characters
