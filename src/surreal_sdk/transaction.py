"""
Transaction support for SurrealDB SDK.

Provides atomic transaction handling for both HTTP and WebSocket connections.
"""

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from .exceptions import TransactionError
from .utils import _ESCAPED_ID_UNIT, substitute_params, unescape_record_id

#: A table, relation or field name SurrealQL reads without delimiters.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: A record ID part that is written into the SQL as it is: an identifier, or an
#: integer (``users:1`` stays the integer ID 1, as it always was here).
_INLINE_ID_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|0|[1-9][0-9]{0,17})$")

#: ID generators SurrealQL accepts after ``table:`` (``CREATE users:ulid()``).
_ID_GENERATORS = frozenset({"rand()", "ulid()", "uuid()"})


def _validate_identifier(name: str, context: str) -> None:
    """Raise ``ValueError`` unless ``name`` is a plain SurrealQL identifier."""
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid {context}: {name!r} (expected an identifier)")


def _raw_record_id(id_part: str, thing: str, context: str) -> str | None:
    r"""Return the raw ID of a delimited ID part, ``None`` when it is not delimited.

    Two delimited forms are read back: backticks, as ``format_thing()`` writes
    them (``\\`` and ``\``` inside), and Unicode angle brackets without
    escapes. A delimited form that is not closed, or that holds its own closing
    delimiter without an escape, raises ``ValueError``.
    """
    if id_part.startswith("`"):
        units = _ESCAPED_ID_UNIT.findall(id_part[1:-1]) if len(id_part) >= 2 and id_part.endswith("`") else ["`"]
        if "`" in units:
            raise ValueError(f"Invalid {context}: {thing!r} (malformed backtick-delimited ID)")
        return unescape_record_id(id_part)
    if id_part.startswith("\u27e8"):
        body = id_part[1:-1]
        if len(id_part) < 2 or not id_part.endswith("\u27e9") or "\u27e9" in body or "\\" in body:
            raise ValueError(f"Invalid {context}: {thing!r} (malformed angle-bracket ID)")
        return body
    return None


def _record_target(thing: str, context: str, binding: str, taken: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Render a ``table`` or ``table:id`` reference for a queued statement.

    The table must be an identifier. An ID that is an identifier, an integer or
    ``rand()``/``ulid()``/``uuid()`` is written as it is; any other ID is bound
    as a string parameter and rendered ``type::record('table', $binding)``, so
    no part of it is ever read as SurrealQL. A delimited ID (``users:`a b```, as
    ``format_thing()`` writes it, or ``users:\u27e8a b\u27e9``) is read back first and
    bound, so it is never escaped twice. Array and object IDs are not accepted.

    Args:
        thing: The ``table`` or ``table:id`` reference.
        context: The argument name, for the error message.
        binding: The preferred parameter name for a bound ID.
        taken: Parameters the statement already binds; the name avoids them.

    Returns:
        The SQL fragment and the parameters it binds (empty when written as is).

    Raises:
        ValueError: If the reference is malformed.
    """
    if not isinstance(thing, str):
        raise ValueError(f"Invalid {context}: {thing!r} (expected a 'table' or 'table:id' string)")
    table, sep, id_part = thing.partition(":")
    if not _IDENTIFIER_RE.match(table):
        raise ValueError(f"Invalid {context}: {thing!r} (the table must be an identifier)")
    if not sep:
        return table, {}
    if not id_part:
        raise ValueError(f"Invalid {context}: {thing!r} (empty record ID)")
    raw = _raw_record_id(id_part, thing, context)
    if raw is None:
        if id_part in _ID_GENERATORS or _INLINE_ID_RE.match(id_part):
            return f"{table}:{id_part}", {}
        if id_part[0] in "[{":
            raise ValueError(f"Invalid {context}: {thing!r} (array and object IDs are not supported; use query())")
        raw = id_part
    if not raw:
        raise ValueError(f"Invalid {context}: {thing!r} (empty record ID)")
    name = binding
    suffix = 0
    while name in taken:
        suffix += 1
        name = f"{binding}{suffix}"
    return f"type::record('{table}', ${name})", {name: raw}


def _validate_thing(thing: str, context: str = "thing") -> None:
    """Raise ``ValueError`` unless ``thing`` is a valid ``table`` or ``table:id`` reference."""
    _record_target(thing, context, "_ref", {})


if TYPE_CHECKING:
    from .connection.base import BaseSurrealConnection
    from .connection.http import HTTPConnection
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

    @property
    def defers_results(self) -> bool:
        """Whether per-statement results are deferred until commit.

        HTTP transactions batch statements and return an *empty placeholder*
        response from each operation immediately; the real per-statement
        results only exist once :meth:`commit` runs. WebSocket transactions
        execute each statement on the server right away and return the real
        result.

        Callers that inspect a statement's result (e.g. to detect a
        permission-denied no-op, where SurrealDB returns zero affected rows)
        must skip that check when results are deferred — an empty placeholder
        is indistinguishable from a genuinely empty result. Defaults to
        ``False`` (immediate results); overridden by buffering transports.
        """
        return False

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
    async def upsert(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Upsert records within the transaction (create or update)."""
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

    While it is open, the transaction counts as using its ``HTTPConnection``
    even though nothing is sent yet, so that a connection being retired (see
    ``HTTPConnection._drain_and_close``) stays open until the commit.
    """

    def __init__(self, connection: "BaseSurrealConnection"):
        super().__init__(connection)
        # The HTTP connection this open transaction is counted on, and the task
        # it was counted for (see ``_hold_connection``).
        self._held: HTTPConnection | None = None
        self._held_for: asyncio.Task[Any] | None = None

    @property
    def defers_results(self) -> bool:
        """HTTP transactions buffer statements; results are known only at commit."""
        return True

    async def _begin(self) -> None:
        """Mark transaction as active (no server call needed for HTTP)."""
        if self._active:
            raise TransactionError("Transaction already active")
        self._active = True
        self._statements = []
        self._hold_connection()

    def _hold_connection(self) -> None:
        """Count this transaction as using its connection until it ends."""
        from .connection.http import HTTPConnection

        if self._held is None and isinstance(self._connection, HTTPConnection):
            self._held_for = self._connection._transaction_opened()
            self._held = self._connection

    async def _release_connection(self) -> None:
        """Stop counting this transaction on its connection (idempotent)."""
        held, self._held = self._held, None
        if held is not None:
            await held._transaction_ended(self._held_for)
            self._held_for = None

    async def commit(self) -> "QueryResponse":
        """Execute all queued statements atomically.

        Every statement's bindings are renamed ``$k`` → ``$tx_<i>_k`` (``i`` being its
        position) and sent together in one query. Only references to a key the
        statement binds are renamed; built-ins such as ``$auth`` or ``$session`` and
        unbound references stay as written. The scan is textual, so a bound ``$k``
        written inside a quoted string literal is renamed too.
        """
        if not self.is_active:
            raise TransactionError("Transaction not active")

        try:
            return await self._send_statements()
        finally:
            await self._release_connection()

    async def _send_statements(self) -> "QueryResponse":
        """Send the queued statements as one ``BEGIN``/``COMMIT`` query (see ``commit``)."""
        from .types import QueryResponse

        if not self._statements:
            # Empty transaction, just mark as committed
            self._committed = True
            self._active = False
            return QueryResponse(results=[], raw=[])

        # Build batched query with BEGIN/COMMIT wrapper
        sql_parts = ["BEGIN TRANSACTION;"]
        all_vars: dict[str, Any] = {}

        for i, stmt in enumerate(self._statements):
            # Namespace each statement's bindings ($k -> $tx_<i>_k) so two statements
            # can bind the same name to different values in the one batched query.
            # substitute_params() renames whole identifiers only: a key that prefixes
            # another reference ($a vs $auth, $session, $tenant…) leaves it alone.
            # It is also a single pass, so a renamed reference is never scanned
            # again: a field named `tx` cannot reach into `$tx_0_amount`.
            for key, val in stmt.vars.items():
                all_vars[f"tx_{i}_{key}"] = val
            sql_parts.append(substitute_params(stmt.sql, {key: f"$tx_{i}_{key}" for key in stmt.vars}))

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
        await self._release_connection()

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
        """Queue a create operation (see :func:`_record_target` for ``thing``)."""
        from .types import RecordResponse

        target, ref = _record_target(thing, "thing", "_ref", data or {})
        if data:
            # Build CREATE statement with data
            fields = ", ".join(f"{k} = ${k}" for k in data)
            self._queue_statement(f"CREATE {target} SET {fields};", {**data, **ref})
        else:
            self._queue_statement(f"CREATE {target};", ref)
        return RecordResponse(record=None, raw=None)

    async def insert(self, table: str, data: list[dict[str, Any]] | dict[str, Any]) -> "RecordsResponse":
        """Queue an insert operation (``table`` must be an identifier)."""
        from .types import RecordsResponse

        _validate_identifier(table, "table")
        if isinstance(data, dict):
            data = [data]

        for i, record in enumerate(data):
            fields = ", ".join(f"{k} = $r{i}_{k}" for k in record)
            sql = f"CREATE {table} SET {fields};"
            vars_with_prefix = {f"r{i}_{k}": v for k, v in record.items()}
            self._queue_statement(sql, vars_with_prefix)

        return RecordsResponse(records=[], raw=[])

    async def update(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Queue an update operation (see :func:`_record_target` for ``thing``)."""
        from .types import RecordsResponse

        target, ref = _record_target(thing, "thing", "_ref", data)
        fields = ", ".join(f"{k} = ${k}" for k in data)
        self._queue_statement(f"UPDATE {target} SET {fields};", {**data, **ref})
        return RecordsResponse(records=[], raw=[])

    async def upsert(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Queue an upsert operation, create or update (see :func:`_record_target` for ``thing``)."""
        from .types import RecordsResponse

        target, ref = _record_target(thing, "thing", "_ref", data)
        fields = ", ".join(f"{k} = ${k}" for k in data)
        self._queue_statement(f"UPSERT {target} SET {fields};", {**data, **ref})
        return RecordsResponse(records=[], raw=[])

    async def merge(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Queue a merge operation (see :func:`_record_target` for ``thing``)."""
        from .types import RecordsResponse

        target, ref = _record_target(thing, "thing", "_ref", data)
        fields = ", ".join(f"{k} = ${k}" for k in data)
        self._queue_statement(f"UPDATE {target} MERGE {{ {fields} }};", {**data, **ref})
        return RecordsResponse(records=[], raw=[])

    async def delete(self, thing: str) -> "DeleteResponse":
        """Queue a delete operation (see :func:`_record_target` for ``thing``)."""
        from .types import DeleteResponse

        target, ref = _record_target(thing, "thing", "_ref", {})
        self._queue_statement(f"DELETE {target};", ref)
        return DeleteResponse(deleted=[], raw=[])

    async def relate(
        self,
        from_thing: str,
        relation: str,
        to_thing: str,
        data: dict[str, Any] | None = None,
    ) -> "RecordResponse":
        """Queue a relate operation.

        ``relation`` must be an identifier; ``from_thing`` and ``to_thing`` follow
        :func:`_record_target`. A bound end is parenthesized, which ``RELATE``
        requires for an expression.
        """
        from .types import RecordResponse

        _validate_identifier(relation, "relation")
        bindings: dict[str, Any] = dict(data or {})
        source, source_ref = _record_target(from_thing, "from_thing", "_from", bindings)
        bindings.update(source_ref)
        target, target_ref = _record_target(to_thing, "to_thing", "_to", bindings)
        bindings.update(target_ref)
        if source_ref:
            source = f"({source})"
        if target_ref:
            target = f"({target})"
        if data:
            fields = ", ".join(f"{k} = ${k}" for k in data)
            sql = f"RELATE {source}->{relation}->{target} SET {fields};"
        else:
            sql = f"RELATE {source}->{relation}->{target};"
        self._queue_statement(sql, bindings)
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
        _validate_thing(thing, "thing")
        return await self._connection.create(thing, data)

    async def insert(self, table: str, data: list[dict[str, Any]] | dict[str, Any]) -> "RecordsResponse":
        """Execute insert immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        _validate_identifier(table, "table")
        return await self._connection.insert(table, data)

    async def update(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Execute update immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        _validate_thing(thing, "thing")
        return await self._connection.update(thing, data)

    async def upsert(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Execute upsert immediately within transaction (create or update)."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        _validate_thing(thing, "thing")
        return await self._connection.upsert(thing, data)

    async def merge(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Execute merge immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        _validate_thing(thing, "thing")
        return await self._connection.merge(thing, data)

    async def delete(self, thing: str) -> "DeleteResponse":
        """Execute delete immediately within transaction."""
        if not self.is_active:
            raise TransactionError("Transaction not active")
        _validate_thing(thing, "thing")
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
        _validate_thing(from_thing, "from_thing")
        _validate_identifier(relation, "relation")
        _validate_thing(to_thing, "to_thing")
        return await self._connection.relate(from_thing, relation, to_thing, data)
