"""
Transaction support for SurrealDB SDK.

Provides atomic transaction handling for both HTTP and WebSocket connections.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from .exceptions import TransactionError

if TYPE_CHECKING:
    from .connection.base import BaseSurrealConnection
    from .types import (
        DeleteResponse,
        QueryResponse,
        RecordResponse,
        RecordsResponse,
    )


@dataclass
class TransactionStatement:
    """A single statement queued in a transaction."""

    sql: str
    vars: dict[str, Any] = field(default_factory=dict)


class BaseTransaction(ABC):
    """
    Abstract base class for transaction handling.

    Provides a unified interface for atomic operations that works
    across both HTTP and WebSocket connections.

    Usage:
        async with conn.transaction() as tx:
            await tx.update("players:abc", {"is_ready": True})
            await tx.update("game_tables:xyz", {"ready_count": 1})
            # Auto-commit on success, auto-rollback on exception
    """

    def __init__(self, connection: "BaseSurrealConnection"):
        self._connection = connection
        self._statements: list[TransactionStatement] = []
        self._committed = False
        self._rolled_back = False
        self._active = False

    @property
    def is_active(self) -> bool:
        """Check if transaction is active."""
        return self._active and not self._committed and not self._rolled_back

    @property
    def is_committed(self) -> bool:
        """Check if transaction was committed."""
        return self._committed

    @property
    def is_rolled_back(self) -> bool:
        """Check if transaction was rolled back."""
        return self._rolled_back

    async def __aenter__(self) -> Self:
        """Begin transaction on context entry."""
        await self._begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> bool:
        """Commit on success, rollback on exception."""
        if exc_type is not None:
            await self.rollback()
            return False  # Re-raise exception
        await self.commit()
        return False

    @abstractmethod
    async def _begin(self) -> None:
        """Begin the transaction."""
        ...

    @abstractmethod
    async def commit(self) -> "QueryResponse":
        """Commit the transaction."""
        ...

    @abstractmethod
    async def rollback(self) -> None:
        """Rollback the transaction."""
        ...

    # Transaction operations (to be implemented by subclasses)

    @abstractmethod
    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> "QueryResponse":
        """Execute a query within the transaction."""
        ...

    @abstractmethod
    async def create(self, thing: str, data: dict[str, Any] | None = None) -> "RecordResponse":
        """Create a record within the transaction."""
        ...

    @abstractmethod
    async def insert(self, table: str, data: list[dict[str, Any]] | dict[str, Any]) -> "RecordsResponse":
        """Insert records within the transaction."""
        ...

    @abstractmethod
    async def update(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Update records within the transaction."""
        ...

    @abstractmethod
    async def merge(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Merge data into records within the transaction."""
        ...

    @abstractmethod
    async def delete(self, thing: str) -> "DeleteResponse":
        """Delete records within the transaction."""
        ...

    @abstractmethod
    async def relate(
        self,
        from_thing: str,
        relation: str,
        to_thing: str,
        data: dict[str, Any] | None = None,
    ) -> "RecordResponse":
        """Create a relation within the transaction."""
        ...


class HTTPTransaction(BaseTransaction):
    """
    HTTP-based transaction that batches statements.

    Since HTTP is stateless, all statements are collected and
    executed as a single atomic query on commit.

    The statements are wrapped in BEGIN TRANSACTION / COMMIT TRANSACTION
    and sent as a single request.
    """

    async def _begin(self) -> None:
        """Mark transaction as active (no server call needed for HTTP)."""
        if self._active:
            raise TransactionError("Transaction already active")
        self._active = True
        self._statements = []

    async def commit(self) -> "QueryResponse":
        """Execute all queued statements atomically."""
        from .types import QueryResponse

        if not self.is_active:
            raise TransactionError("Transaction not active")

        if not self._statements:
            # Empty transaction, just mark as committed
            self._committed = True
            self._active = False
            return QueryResponse(results=[], raw=[])

        # Build batched query with BEGIN/COMMIT wrapper
        sql_parts = ["BEGIN TRANSACTION;"]
        all_vars: dict[str, Any] = {}

        for i, stmt in enumerate(self._statements):
            sql_parts.append(stmt.sql)
            # Namespace variables to avoid conflicts between statements
            for key, val in stmt.vars.items():
                namespaced_key = f"tx_{i}_{key}"
                all_vars[namespaced_key] = val
                # Replace variable reference in SQL
                stmt.sql = stmt.sql.replace(f"${key}", f"${namespaced_key}")
            sql_parts[i + 1] = stmt.sql  # Update with namespaced vars

        sql_parts.append("COMMIT TRANSACTION;")
        full_sql = "\n".join(sql_parts)

        try:
            result = await self._connection.query(full_sql, all_vars)
            self._committed = True
            self._active = False
            return result
        except Exception as e:
            self._active = False
            raise TransactionError(f"Transaction commit failed: {e}")

    async def rollback(self) -> None:
        """Discard queued statements (no server call needed for HTTP)."""
        self._statements = []
        self._rolled_back = True
        self._active = False

    def _queue_statement(self, sql: str, vars: dict[str, Any] | None = None) -> None:
        """Queue a statement for later execution."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        self._statements.append(TransactionStatement(sql=sql, vars=vars or {}))

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> "QueryResponse":
        """Queue a query for execution on commit."""
        from .types import QueryResponse

        self._queue_statement(sql, vars)
        # Return empty response since actual execution happens on commit
        return QueryResponse(results=[], raw=[])

    async def create(self, thing: str, data: dict[str, Any] | None = None) -> "RecordResponse":
        """Queue a create operation."""
        from .types import RecordResponse

        if data:
            # Build CREATE statement with data
            fields = ", ".join(f"{k} = ${k}" for k in data.keys())
            sql = f"CREATE {thing} SET {fields};"
            self._queue_statement(sql, data)
        else:
            sql = f"CREATE {thing};"
            self._queue_statement(sql)
        return RecordResponse(record=None, raw=None)

    async def insert(self, table: str, data: list[dict[str, Any]] | dict[str, Any]) -> "RecordsResponse":
        """Queue an insert operation."""
        from .types import RecordsResponse

        if isinstance(data, dict):
            data = [data]

        for i, record in enumerate(data):
            fields = ", ".join(f"{k} = $r{i}_{k}" for k in record.keys())
            sql = f"CREATE {table} SET {fields};"
            vars_with_prefix = {f"r{i}_{k}": v for k, v in record.items()}
            self._queue_statement(sql, vars_with_prefix)

        return RecordsResponse(records=[], raw=[])

    async def update(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Queue an update operation."""
        from .types import RecordsResponse

        fields = ", ".join(f"{k} = ${k}" for k in data.keys())
        sql = f"UPDATE {thing} SET {fields};"
        self._queue_statement(sql, data)
        return RecordsResponse(records=[], raw=[])

    async def merge(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Queue a merge operation."""
        from .types import RecordsResponse

        fields = ", ".join(f"{k} = ${k}" for k in data.keys())
        sql = f"UPDATE {thing} MERGE {{ {fields} }};"
        self._queue_statement(sql, data)
        return RecordsResponse(records=[], raw=[])

    async def delete(self, thing: str) -> "DeleteResponse":
        """Queue a delete operation."""
        from .types import DeleteResponse

        sql = f"DELETE {thing};"
        self._queue_statement(sql)
        return DeleteResponse(deleted=[], raw=[])

    async def relate(
        self,
        from_thing: str,
        relation: str,
        to_thing: str,
        data: dict[str, Any] | None = None,
    ) -> "RecordResponse":
        """Queue a relate operation."""
        from .types import RecordResponse

        if data:
            fields = ", ".join(f"{k} = ${k}" for k in data.keys())
            sql = f"RELATE {from_thing}->{relation}->{to_thing} SET {fields};"
            self._queue_statement(sql, data)
        else:
            sql = f"RELATE {from_thing}->{relation}->{to_thing};"
            self._queue_statement(sql)
        return RecordResponse(record=None, raw=None)


class WebSocketTransaction(BaseTransaction):
    """
    WebSocket-based transaction with server-side state.

    Uses actual BEGIN/COMMIT/ROLLBACK commands since
    WebSocket maintains session state across requests.

    Operations are executed immediately within the transaction context.
    """

    async def _begin(self) -> None:
        """Send BEGIN TRANSACTION to server."""
        if self._active:
            raise TransactionError("Transaction already active")
        await self._connection.query("BEGIN TRANSACTION;")
        self._active = True

    async def commit(self) -> "QueryResponse":
        """Send COMMIT to server."""
        if not self.is_active:
            raise TransactionError("Transaction not active")

        try:
            result = await self._connection.query("COMMIT TRANSACTION;")
            self._committed = True
            self._active = False
            return result
        except Exception as e:
            self._active = False
            raise TransactionError(f"Commit failed: {e}")

    async def rollback(self) -> None:
        """Send ROLLBACK to server."""
        if not self.is_active:
            return

        try:
            await self._connection.query("CANCEL TRANSACTION;")
        except Exception:
            pass  # Best effort rollback
        finally:
            self._rolled_back = True
            self._active = False

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> "QueryResponse":
        """Execute query immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        return await self._connection.query(sql, vars)

    async def create(self, thing: str, data: dict[str, Any] | None = None) -> "RecordResponse":
        """Execute create immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        return await self._connection.create(thing, data)

    async def insert(self, table: str, data: list[dict[str, Any]] | dict[str, Any]) -> "RecordsResponse":
        """Execute insert immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        return await self._connection.insert(table, data)

    async def update(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Execute update immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        return await self._connection.update(thing, data)

    async def merge(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Execute merge immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        return await self._connection.merge(thing, data)

    async def delete(self, thing: str) -> "DeleteResponse":
        """Execute delete immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        return await self._connection.delete(thing)

    async def relate(
        self,
        from_thing: str,
        relation: str,
        to_thing: str,
        data: dict[str, Any] | None = None,
    ) -> "RecordResponse":
        """Execute relate immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        return await self._connection.relate(from_thing, relation, to_thing, data)
