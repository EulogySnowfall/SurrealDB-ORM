"""
Transaction support for SurrealDB SDK.

Provides atomic transaction handling for both HTTP and WebSocket connections.
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Container
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Self

from .exceptions import TransactionConflictError, TransactionError
from .protocol.cbor import _ESCAPED_ID_UNIT, unescape_record_id

# A whole ``$name`` parameter reference. ASCII only, like SurrealDB's identifier
# lexer: ``$a-b`` is ``$a`` followed by ``-b``, and a non-ASCII name is rejected.
_PARAM_REFERENCE_RE = re.compile(r"\$(\w+)", re.ASCII)


def _namespace_params(sql: str, keys: Container[str], prefix: str) -> str:
    """Rename each whole ``$key`` reference in *sql* to ``$<prefix><key>``, in one pass.

    Only a reference whose *entire* name is in *keys* is renamed. A key that is a
    prefix of a longer reference — a built-in such as ``$auth`` or ``$session``, or
    a parameter nobody binds — leaves that reference as written. The scan runs once
    over the original text, so a reference just renamed is never matched again by a
    later key (a key ``tx`` cannot reach into ``$tx_0_amount``).

    The scan is textual: a bound ``$key`` written inside a quoted string literal is
    renamed too.

    Args:
        sql: SurrealQL statement containing ``$name`` references
        keys: Names the statement binds
        prefix: Text inserted between ``$`` and each bound name

    Returns:
        The statement with every bound reference renamed
    """

    def _rename(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"${prefix}{name}" if name in keys else match.group(0)

    return _PARAM_REFERENCE_RE.sub(_rename, sql)


#: A table, relation or field name SurrealQL reads without delimiters.
#: Always used with ``fullmatch``: ``$`` would also accept a trailing newline.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A record ID part that SurrealQL reads as an integer when written inline
#: (``users:1``, ``users:-5``), provided it fits in a signed 64-bit integer;
#: outside that range the server reads it as a string ID.
_INTEGER_ID_RE = re.compile(r"-?(?:0|[1-9][0-9]*)")

_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

#: ID generators SurrealQL accepts after ``table:`` (``CREATE users:ulid()``).
_ID_GENERATORS = frozenset({"rand()", "ulid()", "uuid()"})


def _validate_identifier(name: str, context: str) -> None:
    """Raise ``ValueError`` unless ``name`` is a plain SurrealQL identifier."""
    if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
        raise ValueError(f"Invalid {context}: {name!r} (expected an identifier)")


def _validate_keys(data: dict[str, Any], context: str = "data") -> None:
    """Raise ``ValueError`` unless every key of ``data`` is a plain SurrealQL identifier.

    ``HTTPTransaction`` writes each key into the queued statement as the field
    name and as the name of its parameter (``k = $k``), so a key must be valid
    in both places. The value is always bound and is never checked here.
    """
    for key in data:
        if not isinstance(key, str) or not _IDENTIFIER_RE.fullmatch(key):
            raise ValueError(f"Invalid {context} key: {key!r} (expected an identifier)")


def _raw_record_id(id_part: str, thing: str, context: str) -> str | None:
    r"""Return the raw ID of a delimited ID part, ``None`` when it is not delimited.

    Two delimited forms are read back: backticks, as ``format_thing()`` writes
    them (``\\`` and ``\``` inside), and Unicode angle brackets without
    escapes. A delimited form that is not closed, or that holds its own closing
    delimiter without an escape, raises ``ValueError``.
    """
    if id_part.startswith("`"):
        closed = len(id_part) >= 2 and id_part.endswith("`")
        units = _ESCAPED_ID_UNIT.findall(id_part[1:-1]) if closed else ["`"]
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

    The table must be an identifier. An ID that is an identifier, an integer in
    the signed 64-bit range (``users:-5``, as SurrealQL reads it inline) or
    ``rand()``/``ulid()``/``uuid()`` is written as it is; any other ID is bound
    as a string parameter and rendered ``type::thing('table', $binding)``, so
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
    if not _IDENTIFIER_RE.fullmatch(table):
        raise ValueError(f"Invalid {context}: {thing!r} (the table must be an identifier)")
    if not sep:
        return table, {}
    if not id_part:
        raise ValueError(f"Invalid {context}: {thing!r} (empty record ID)")
    raw = _raw_record_id(id_part, thing, context)
    if raw is None:
        if id_part in _ID_GENERATORS or _IDENTIFIER_RE.fullmatch(id_part):
            return f"{table}:{id_part}", {}
        if len(id_part) <= 20 and _INTEGER_ID_RE.fullmatch(id_part) and _I64_MIN <= int(id_part) <= _I64_MAX:
            return f"{table}:{int(id_part)}", {}
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
    return f"type::thing('{table}', ${name})", {name: raw}


def _validate_thing(thing: str, context: str = "thing") -> None:
    """Raise ``ValueError`` unless ``thing`` is a valid ``table`` or ``table:id`` reference."""
    _record_target(thing, context, "_ref", {})


if TYPE_CHECKING:
    from .connection.base import BaseSurrealConnection
    from .types import (
        DeleteResponse,
        QueryResponse,
        RecordResponse,
        RecordsResponse,
    )


#: Errors SurrealDB reports for the statements a failed transaction cancelled,
#: rather than for the statement that failed it. 2.x sends the first for every
#: cancelled statement; the second is kept for servers that report the aborted
#: ``COMMIT`` itself.
_CANCELLED_STATEMENT_ERRORS = ("not executed due to a failed transaction", "Cannot COMMIT")


def _failed_statement_error(response: "QueryResponse") -> tuple[str, bool] | None:
    """The server's error for the statement that failed a batched transaction.

    Once a statement fails, SurrealDB reports every other statement of the
    transaction as not executed; those messages say nothing about the cause, so
    the first error that is not one of them is returned, with ``True``. When
    there are only such messages, the first one is returned with ``False``
    (cause unknown). ``None`` when every statement succeeded.
    """
    errors = [str(statement.result) for statement in response.results if statement.is_error]
    if not errors:
        return None
    for error in errors:
        if not any(marker in error for marker in _CANCELLED_STATEMENT_ERRORS):
            return error, True
    return errors[0], False


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

    ``create``, ``insert``, ``update``, ``upsert``, ``merge`` and ``relate``
    write each key of ``data`` into the statement as a field name, so every
    key must be a plain identifier (``[A-Za-z_][A-Za-z0-9_]*``); any other key
    raises ``ValueError`` at the call and nothing is queued. Values are always
    bound as parameters.
    """

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

    async def commit(self) -> "QueryResponse":
        """Execute all queued statements atomically.

        Every statement's bindings are renamed ``$k`` → ``$tx_<i>_k`` (``i`` being its
        position) and sent together in one query. Only references to a key the
        statement binds are renamed; built-ins such as ``$auth`` or ``$session`` and
        unbound references stay as written. The scan is textual, so a bound ``$k``
        written inside a quoted string literal is renamed too.

        Returns:
            The server's response, one result per statement.

        Raises:
            TransactionConflictError: The statement that failed the batch reported
                a retryable conflict.
            TransactionError: The request failed (the original exception is
                chained), or the server rejected a statement; the message carries
                that statement's own error. Nothing was written, ``is_committed``
                is ``False`` and the transaction is no longer active.
        """
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
            # Namespace each statement's bindings ($k -> $tx_<i>_k) so two statements
            # can bind the same name to different values in the one batched query.
            # Whole identifiers only, in a single pass: a key that prefixes another
            # reference ($a vs $auth, $session, $tenant…) leaves it alone, and a field
            # named `tx` cannot reach into the `$tx_0_amount` just emitted. The queued
            # statement itself is left untouched.
            for key, val in stmt.vars.items():
                all_vars[f"tx_{i}_{key}"] = val
            sql_parts.append(_namespace_params(stmt.sql, stmt.vars, f"tx_{i}_"))

        sql_parts.append("COMMIT TRANSACTION;")
        full_sql = "\n".join(sql_parts)

        try:
            result = await self._connection.query(full_sql, all_vars)
        except Exception as e:
            self._active = False
            raise TransactionError(f"Transaction commit failed: {e}") from e

        self._active = False
        # A statement the server rejects does not fail the request: it comes back
        # inside a successful response as ``status: ERR``, the server cancels the
        # rest of the batch, and nothing is written. Without this check the commit
        # looked successful.
        failure = _failed_statement_error(result)
        if failure is not None:
            error, is_cause = failure
            message = f"Transaction commit failed: {error}"
            # A cancellation message says "failed transaction", which would read as a
            # retryable conflict; only the failing statement's own error is classified.
            if is_cause and TransactionConflictError.is_conflict_error(Exception(error)):
                raise TransactionConflictError(message)
            raise TransactionError(message)
        self._committed = True
        return result

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
        """Queue a create operation (see :func:`_record_target` for ``thing``)."""
        from .types import RecordResponse

        _validate_keys(data or {})
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
        # Check every record first, so a bad key in a later one queues nothing.
        for record in data:
            _validate_keys(record)

        for i, record in enumerate(data):
            fields = ", ".join(f"{k} = $r{i}_{k}" for k in record)
            sql = f"CREATE {table} SET {fields};"
            vars_with_prefix = {f"r{i}_{k}": v for k, v in record.items()}
            self._queue_statement(sql, vars_with_prefix)

        return RecordsResponse(records=[], raw=[])

    async def update(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Queue an update operation (see :func:`_record_target` for ``thing``)."""
        from .types import RecordsResponse

        _validate_keys(data)
        target, ref = _record_target(thing, "thing", "_ref", data)
        fields = ", ".join(f"{k} = ${k}" for k in data)
        self._queue_statement(f"UPDATE {target} SET {fields};", {**data, **ref})
        return RecordsResponse(records=[], raw=[])

    async def upsert(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Queue an upsert operation, create or update (see :func:`_record_target` for ``thing``)."""
        from .types import RecordsResponse

        _validate_keys(data)
        target, ref = _record_target(thing, "thing", "_ref", data)
        fields = ", ".join(f"{k} = ${k}" for k in data)
        self._queue_statement(f"UPSERT {target} SET {fields};", {**data, **ref})
        return RecordsResponse(records=[], raw=[])

    async def merge(self, thing: str, data: dict[str, Any]) -> "RecordsResponse":
        """Queue a merge operation (see :func:`_record_target` for ``thing``)."""
        from .types import RecordsResponse

        _validate_keys(data)
        target, ref = _record_target(thing, "thing", "_ref", data)
        # MERGE takes an object literal; ``{ k = $k }`` would be a block of comparisons.
        fields = ", ".join(f"{k}: ${k}" for k in data)
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
        _validate_keys(data or {})
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
