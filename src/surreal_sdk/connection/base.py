"""
Base Connection Interface for SurrealDB SDK.

Defines the abstract interface that all connection types must implement.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Mapping
from typing import TYPE_CHECKING, Any, Literal, Self

from .. import jsonl
from ..protocol.rpc import RPCRequest, RPCResponse

if TYPE_CHECKING:
    from ..functions import FunctionNamespace
    from ..transaction import BaseTransaction
from ..types import (
    AuthResponse,
    DeleteResponse,
    InfoResponse,
    QueryResponse,
    RecordResponse,
    RecordsResponse,
)


class BaseSurrealConnection(ABC):
    """
    Abstract base class for SurrealDB connections.

    All connection implementations (HTTP, WebSocket) must inherit from this class.
    """

    #: Serialization protocol, set by each transport's ``__init__``.
    protocol: Literal["json", "cbor"]

    def __init__(
        self,
        url: str,
        namespace: str,
        database: str,
        timeout: float = 30.0,
    ):
        """
        Initialize connection parameters.

        Args:
            url: SurrealDB server URL
            namespace: Target namespace
            database: Target database
            timeout: Request timeout in seconds
        """
        self.url = url.rstrip("/")
        self.namespace = namespace
        self.database = database
        self.timeout = timeout
        self._connected = False
        self._authenticated = False
        self._token: str | None = None
        # Incremented whenever the connection's auth state changes (a token is
        # installed or dropped, the connection is closed). A task that started
        # something on the strength of the old state, such as the HTTP
        # transient-401 re-mint, compares it before acting on its result.
        self._auth_generation = 0
        # Incremented whenever the connection may now act as a different
        # principal: every auth change except the HTTP transient-401 re-mint,
        # which only renews the token of the same ``signin()`` arguments. A
        # request rejected under one identity is never replayed under another.
        self._auth_identity = 0

    @property
    def is_connected(self) -> bool:
        """Check if connection is established."""
        return self._connected

    @property
    def is_authenticated(self) -> bool:
        """Check if authenticated."""
        return self._authenticated

    @property
    def token(self) -> str | None:
        """Get the current authentication token."""
        return self._token

    # Abstract methods that must be implemented

    @abstractmethod
    async def connect(self) -> Self:
        """Establish connection to SurrealDB. Returns self for fluent API."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the connection."""
        ...

    @abstractmethod
    async def _send_rpc(self, request: RPCRequest) -> RPCResponse:
        """
        Send an RPC request and receive response.

        Args:
            request: The RPC request to send

        Returns:
            The RPC response
        """
        ...

    def _forget_signin_credentials(self) -> None:
        """Drop any credentials kept to sign in again on this connection's behalf.

        Called whenever the connection's identity changes outside ``signin()``
        (``authenticate()``, ``signup()``), so that nothing later re-creates the
        previous identity. No-op here; ``HTTPConnection`` overrides it to clear
        the arguments its transient-401 recovery replays.
        """
        return None

    def _auth_changed(self, *, same_identity: bool = False) -> None:
        """Record that the connection's auth state changed.

        Bumps ``_auth_generation`` and, unless *same_identity* (a re-mint of the
        same ``signin()`` arguments), ``_auth_identity`` too.
        """
        self._auth_generation += 1
        if not same_identity:
            self._auth_identity += 1

    # Context manager support

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    # High-level API methods

    async def rpc(self, method: str, params: list[Any] | dict[str, Any] | None = None) -> Any:
        """
        Execute an RPC call.

        Args:
            method: RPC method name
            params: Method parameters

        Returns:
            The result from SurrealDB

        Raises:
            QueryError: If the RPC call fails
        """
        from ..exceptions import QueryError, TableNotFoundError

        request = RPCRequest(method=method, params=params or [])
        response = await self._send_rpc(request)

        if response.is_error:
            message = response.error.message if response.error else "Unknown error"
            code = response.error.code if response.error else None
            if TableNotFoundError.is_table_not_found(message):
                raise TableNotFoundError(message=message, code=code)
            raise QueryError(message=message, code=code)

        return response.result

    async def signin(
        self,
        user: str | None = None,
        password: str | None = None,
        namespace: str | None = None,
        database: str | None = None,
        access: str | None = None,
        **credentials: Any,
    ) -> AuthResponse:
        """
        Authenticate with SurrealDB.

        Args:
            user: Username (for root/namespace/database auth)
            password: Password (for root/namespace/database auth)
            namespace: Optional namespace scope
            database: Optional database scope
            access: Optional access method (for record access auth)
            **credentials: Additional credentials for record access (email, password, etc.)

        Returns:
            AuthResponse with token and success status
        """
        from ..exceptions import AuthenticationError

        params: dict[str, Any] = {}
        if user:
            params["user"] = user
        if password:
            params["pass"] = password
        if namespace:
            params["ns"] = namespace
        if database:
            params["db"] = database
        if access:
            params["ac"] = access
        # Add any additional credentials for record access
        params.update(credentials)

        try:
            result = await self.rpc("signin", params)
            response = AuthResponse.from_rpc_result(result)
            if response.token:
                self._token = response.token
            self._authenticated = response.success
            self._auth_changed()
            return response
        except Exception as e:
            raise AuthenticationError(f"Authentication failed: {e}")

    async def signup(
        self,
        namespace: str,
        database: str,
        access: str,
        **credentials: Any,
    ) -> AuthResponse:
        """
        Sign up a new user.

        The connection switches to the new user's token, and credentials kept
        from an earlier ``signin()`` are dropped (see ``authenticate()``).

        Args:
            namespace: Namespace
            database: Database
            access: Access method
            **credentials: Additional credentials (email, password, etc.)

        Returns:
            AuthResponse with token and success status
        """
        params = {
            "ns": namespace,
            "db": database,
            "ac": access,
            **credentials,
        }
        result = await self.rpc("signup", params)
        # The connection now speaks for the new record user, not for whoever
        # signed in before.
        self._forget_signin_credentials()
        response = AuthResponse.from_rpc_result(result)
        if response.token:
            self._token = response.token
        self._authenticated = response.success
        self._auth_changed()
        return response

    async def authenticate(self, token: str) -> AuthResponse:
        """
        Authenticate using an existing JWT token.

        This validates the token with SurrealDB and sets the connection's
        auth state so that subsequent queries run under the token's identity.
        Credentials kept from an earlier ``signin()`` are dropped, whether or
        not the token is accepted: once the token expires or is rejected,
        requests fail instead of signing in again with them.

        Args:
            token: JWT token from a previous signup/signin

        Returns:
            AuthResponse with success status
        """
        from ..exceptions import AuthenticationError

        try:
            result = await self.rpc("authenticate", [token])
            self._token = token
            self._authenticated = True
            return AuthResponse(token=token, success=True, raw=result)
        except Exception as e:
            self._token = None
            self._authenticated = False
            raise AuthenticationError(f"Token authentication failed: {e}") from e
        finally:
            # Either way the connection no longer holds the identity of an
            # earlier ``signin()``: credentials kept for it must not be replayed
            # when *token* is later rejected. Cleared only after the RPC, so the
            # request carrying it can still recover from a transient 401.
            self._forget_signin_credentials()
            self._auth_changed()

    async def use(self, namespace: str, database: str) -> None:
        """
        Set the namespace and database to use.

        Args:
            namespace: Target namespace
            database: Target database
        """
        await self.rpc("use", [namespace, database])
        self.namespace = namespace
        self.database = database

    async def info(self) -> InfoResponse:
        """Get information about the current user."""
        result = await self.rpc("info")
        return InfoResponse.from_rpc_result(result)

    async def version(self) -> str:
        """Get SurrealDB server version."""
        result = await self.rpc("version")
        return str(result) if result else ""

    async def ping(self) -> bool:
        """Check if connection is alive."""
        try:
            await self.rpc("ping")
            return True
        except Exception:
            return False

    # Query methods

    async def query(self, sql: str, vars: dict[str, Any] | None = None) -> QueryResponse:
        """
        Execute a SurrealQL query.

        Args:
            sql: SurrealQL query string
            vars: Query variables

        Returns:
            QueryResponse containing results for each statement.
            Callers should check ``response.is_ok`` for per-statement errors.
            SurrealDB 3.0 returns ERR status for non-existent tables instead
            of empty arrays — use ``TableNotFoundError.is_table_not_found()``
            on error results to detect this case.
        """
        result = await self.rpc("query", [sql, vars or {}])
        return QueryResponse.from_rpc_result(result)

    # JSON Lines export

    def iter_records(
        self,
        table: str,
        *,
        batch_size: int = 1000,
        where: str | None = None,
        vars: Mapping[str, Any] | None = None,
        limit: int | None = None,
        _on_query: jsonl.QueryHook | None = None,
    ) -> AsyncGenerator[list[dict[str, Any]], None]:
        """
        Read a table in ``id`` order, one batch of decoded records at a time.

        Each batch is one statement, paged by keyset on ``id``
        (``WHERE id > $_x_after ORDER BY id LIMIT n``), which works for every
        kind of record id. Only the batch being yielded is held. The server
        does not seek to the cursor: each batch scans the table from its start
        (measured on 3.3: about 6 ms per 1000-row batch at the start, about
        200 ms at row 300,000), so a whole-table read slows down as the table
        grows. For a full copy of a large table, use a dump
        (``HTTPConnection.export_database()``). Memory and reply size are
        ``batch_size`` times the size of a row: lower ``batch_size`` for wide
        rows. Use it as
        ``async for batch in conn.iter_records("person"): ...``.

        The arguments are checked when the method is called, before any query.

        Args:
            table: The table to read (an identifier).
            batch_size: Rows per batch (``>= 1``).
            where: A raw SurrealQL condition, written as ``({where})``; its
                values belong in ``vars``. The caller is responsible for it,
                as with ``live_select(where=)``.
            vars: Variables bound with every batch; names starting with
                ``_x_`` are reserved.
            limit: The most records to read (``>= 0``); ``None`` for all.

        Returns:
            An async iterator of batches: non-empty lists of plain ``dict``
            records.

        Raises:
            ValueError: On a JSON-protocol connection, an invalid table name,
                a reserved variable name, or a size below its minimum.
            TypeError: If ``batch_size`` or ``limit`` is not an ``int``.
            TableNotFoundError: While iterating, if the table does not exist.
            QueryError: While iterating, if a batch statement fails. A failed
                request (``QueryError``, ``ConnectionError``,
                ``TimeoutError``) propagates unchanged.
        """
        name, size, cap = jsonl._check_export_args(self, "iter_records", table, batch_size, limit, vars)
        return jsonl._iter_batches(
            self,
            name,
            batch_size=size,
            where_sql=None if where is None else f"({where})",
            vars=dict(vars or {}),
            limit=cap,
            method="iter_records",
            on_query=_on_query,
        )

    async def export_jsonl(
        self,
        table: str,
        dest: jsonl.JsonlDestination,
        *,
        batch_size: int = 1000,
        where: str | None = None,
        vars: Mapping[str, Any] | None = None,
        limit: int | None = None,
        typed: bool = True,
        _on_query: jsonl.QueryHook | None = None,
    ) -> int:
        """
        Write a table to JSON Lines, one record per line, in ``id`` order.

        Reads like :meth:`iter_records` and writes each record with
        :func:`surreal_sdk.jsonl.dumps_record`. File writes (the open and the
        close included) run in a worker thread, once per batch. On an error
        the lines already written stay in ``dest``; nothing is retried.

        An export holds the table's data as stored (a password hash of a
        ``USER`` table, tokens, …), so, as for a dump, a **new** file is
        created with mode ``0o600`` (readable by its owner only); an existing
        file is truncated and keeps its mode. On Windows the mode is ignored.

        Args:
            table: The table to export (an identifier).
            dest: A path (created ``0o600`` or truncated, UTF-8) or a text
                stream, which is written to and left open.
            batch_size: Rows per statement (``>= 1``).
            where: See :meth:`iter_records`.
            vars: See :meth:`iter_records`.
            limit: See :meth:`iter_records`.
            typed: ``True`` (default) writes the lossless tagged form;
                ``False`` plain JSON for external tools (it does not load back
                with the same types).

        Returns:
            int: The number of records written.

        Raises:
            DataFormatError: If a value cannot be written losslessly (a
                geometry, a range, an out-of-range datetime, …).
            ValueError, TypeError, TableNotFoundError, QueryError: As
                :meth:`iter_records`; argument errors are raised before
                ``dest`` is opened.
        """
        name, size, cap = jsonl._check_export_args(self, "export_jsonl", table, batch_size, limit, vars)
        batches = jsonl._iter_batches(
            self,
            name,
            batch_size=size,
            where_sql=None if where is None else f"({where})",
            vars=dict(vars or {}),
            limit=cap,
            method="export_jsonl",
            on_query=_on_query,
        )
        return await jsonl._export(batches, dest, typed=typed)

    # JSON Lines import

    async def import_jsonl(
        self,
        table: str,
        source: jsonl.JsonlSource,
        *,
        batch_size: int = 1000,
        max_batch_bytes: int = 1_048_576,
        on_conflict: Literal["error", "ignore"] = "error",
        raw: bool = False,
        relation: bool | None = None,
        _on_query: jsonl.QueryHook | None = None,
    ) -> jsonl.ImportReport:
        """
        Load JSON Lines into a table, in bounded batches of one ``INSERT`` each.

        Each non-blank line is read with :func:`surreal_sdk.jsonl.loads_record`
        and the rows are sent in batches of at most ``batch_size`` rows and
        ``max_batch_bytes`` bytes of JSON text (a longer single line is sent
        alone). A batch is one statement, so the server writes it whole or not
        at all; the batches before a failure stay written. Files and text
        streams are read in a worker thread. The rows keep their ``id`` key; a
        record id of another table is moved to ``table``.

        The database must exist: a raw SDK connection does not create it (the
        ORM's connections do).

        Args:
            table: The table to write (an identifier).
            source: A path (a ``str`` is a path; UTF-8), a text stream (read
                and left open), or an iterable or async iterable of lines.
            batch_size: Rows per statement (``>= 1``).
            max_batch_bytes: JSON bytes per statement (``>= 1``); the default
                1 MiB stays well under the server's 4 MiB HTTP RPC limit.
            on_conflict: ``"error"`` (default) rejects a batch holding an
                existing id; ``"ignore"`` skips such rows (``INSERT IGNORE``).
            raw: ``True`` prefixes each batch with ``OPTION IMPORT;``: no type
                checks, ``ASSERT``, ``VALUE`` or ``DEFAULT``, and no event. It
                applies to that statement only and needs a system user.
            relation: ``True`` writes edges (``INSERT RELATION``), ``False``
                plain rows. ``None`` (default) reads the table's type once:
                ``TYPE RELATION`` writes edges, ``TYPE NORMAL`` plain rows;
                otherwise a batch whose rows all hold record ``in`` and
                ``out`` fields is written as edges.

        Returns:
            ImportReport: The lines read, rows written and batches accepted.

        Raises:
            ValueError: On a JSON-protocol connection, an invalid table name,
                an ``on_conflict`` other than ``"error"`` / ``"ignore"``, or a
                size below its minimum; before the source is opened.
            TypeError: If ``batch_size`` or ``max_batch_bytes`` is not an
                ``int``.
            DataFormatError: On an invalid line (its number is in the
                message), or when ``relation=None`` meets a batch mixing
                edges and plain rows.
            DataImportError: When a batch is rejected by the server, or its
                request fails (HTTP error, lost connection, timeout: the
                original is ``__cause__``); ``report`` holds the batches
                accepted before it.
            OSError, UnicodeDecodeError: Reading the source fails.
        """
        name, size, max_bytes = jsonl._check_import_args(self, table, batch_size, max_batch_bytes, on_conflict)
        return await jsonl._import(
            self,
            name,
            source,
            batch_size=size,
            max_batch_bytes=max_bytes,
            on_conflict=on_conflict,
            raw=raw,
            relation=relation,
            on_query=_on_query,
        )

    async def select(self, thing: str) -> RecordsResponse:
        """
        Select records from a table or specific record.

        Args:
            thing: Table name or record ID (e.g., "users" or "users:123")

        Returns:
            RecordsResponse containing selected records
        """
        result = await self.rpc("select", [thing])
        return RecordsResponse.from_rpc_result(result)

    async def create(self, thing: str, data: dict[str, Any] | None = None) -> RecordResponse:
        """
        Create a new record.

        Args:
            thing: Table name or record ID
            data: Record data

        Returns:
            RecordResponse containing the created record
        """
        result = await self.rpc("create", [thing, data or {}])
        return RecordResponse.from_rpc_result(result)

    async def insert(self, table: str, data: list[dict[str, Any]] | dict[str, Any]) -> RecordsResponse:
        """
        Insert one or more records.

        Args:
            table: Table name
            data: Record(s) to insert

        Returns:
            RecordsResponse containing inserted records
        """
        result = await self.rpc("insert", [table, data])
        return RecordsResponse.from_rpc_result(result)

    async def update(self, thing: str, data: dict[str, Any]) -> RecordsResponse:
        """
        Update record(s), replacing all fields.

        Args:
            thing: Table name or record ID
            data: New record data

        Returns:
            RecordsResponse containing updated record(s)
        """
        result = await self.rpc("update", [thing, data])
        return RecordsResponse.from_rpc_result(result)

    async def upsert(self, thing: str, data: dict[str, Any]) -> RecordsResponse:
        """
        Upsert record(s) - create if not exists, update if exists.

        This is the recommended method for save operations when you have
        a specific ID and want idempotent behavior.

        Args:
            thing: Table name or record ID
            data: Record data

        Returns:
            RecordsResponse containing upserted record(s)
        """
        result = await self.rpc("upsert", [thing, data])
        return RecordsResponse.from_rpc_result(result)

    async def merge(self, thing: str, data: dict[str, Any]) -> RecordsResponse:
        """
        Merge data into record(s), updating only specified fields.

        Args:
            thing: Table name or record ID
            data: Fields to merge

        Returns:
            RecordsResponse containing updated record(s)
        """
        result = await self.rpc("merge", [thing, data])
        return RecordsResponse.from_rpc_result(result)

    async def patch(self, thing: str, patches: list[dict[str, Any]]) -> RecordsResponse:
        """
        Apply JSON Patch operations to record(s).

        Args:
            thing: Table name or record ID
            patches: List of JSON Patch operations

        Returns:
            RecordsResponse containing updated record(s)
        """
        result = await self.rpc("patch", [thing, patches])
        return RecordsResponse.from_rpc_result(result)

    async def delete(self, thing: str) -> DeleteResponse:
        """
        Delete record(s).

        Args:
            thing: Table name or record ID

        Returns:
            DeleteResponse containing deleted record(s)
        """
        result = await self.rpc("delete", [thing])
        return DeleteResponse.from_rpc_result(result)

    async def relate(
        self,
        from_thing: str,
        relation: str,
        to_thing: str,
        data: dict[str, Any] | None = None,
    ) -> RecordResponse:
        """
        Create a graph relationship between records.

        Args:
            from_thing: Source record ID
            relation: Relation table name
            to_thing: Target record ID
            data: Optional relation data

        Returns:
            RecordResponse containing the created relation record
        """
        params: list[Any] = [from_thing, relation, to_thing]
        if data:
            params.append(data)
        result = await self.rpc("relate", params)
        return RecordResponse.from_rpc_result(result)

    # Transaction support

    @abstractmethod
    def transaction(self) -> "BaseTransaction":
        """
        Create a new transaction context.

        Usage:
            async with conn.transaction() as tx:
                await tx.update("players:abc", {"is_ready": True})
                await tx.update("game_tables:xyz", {"ready_count": 1})
                # Auto-commit on success, auto-rollback on exception

        Returns:
            Transaction context manager
        """
        ...

    # Function call API

    @property
    def fn(self) -> "FunctionNamespace":
        """
        Access SurrealDB function call API.

        Usage:
            # Built-in functions
            result = await conn.fn.math.sqrt(16)
            result = await conn.fn.time.now()

            # Custom user-defined functions
            result = await conn.fn.cast_vote(user_id, table_id, "yes")

        Returns:
            Function namespace for building calls
        """
        from ..functions import FunctionNamespace

        return FunctionNamespace(self)

    async def call(
        self,
        function: str,
        params: dict[str, Any] | None = None,
        return_type: type | None = None,
    ) -> Any:
        """
        Call a SurrealDB function with typed return value.

        This method provides a clean interface for calling custom functions
        with automatic type conversion using Pydantic models or dataclasses.

        Args:
            function: Function name (e.g., "fn::cast_vote" or just "cast_vote")
            params: Named parameters to pass to the function
            return_type: Optional Pydantic model or dataclass to convert result to

        Returns:
            The function result, optionally converted to return_type

        Usage:
            # Without type
            result = await conn.call("fn::cast_vote", {
                "user_id": "users:alice",
                "table_id": "game_tables:xyz",
                "vote": "yes"
            })

            # With typed return
            @dataclass
            class VoteResult:
                success: bool
                new_count: int
                total_votes: int

            result: VoteResult = await conn.call(
                "fn::cast_vote",
                params={"user_id": "users:alice", "table_id": "game_tables:xyz", "vote": "yes"},
                return_type=VoteResult
            )
        """
        # Normalize function name
        if not function.startswith("fn::") and "::" not in function:
            function = f"fn::{function}"

        # Build parameterized query
        if params:
            param_placeholders = ", ".join(f"${key}" for key in params)
            sql = f"RETURN {function}({param_placeholders});"
        else:
            sql = f"RETURN {function}();"

        result = await self.query(sql, params or {})

        # Extract result value
        value = None
        if result.first_result and result.first_result.result is not None:
            value = result.first_result.result

        # Convert to return_type if specified
        if return_type is not None and value is not None:
            return self._convert_to_type(value, return_type)

        return value

    def _convert_to_type(self, value: Any, target_type: type) -> Any:
        """Convert a value to the target type."""
        import dataclasses

        # Check if it's a Pydantic model
        try:
            from pydantic import BaseModel

            if isinstance(target_type, type) and issubclass(target_type, BaseModel):
                if isinstance(value, dict):
                    return target_type(**value)
                return target_type.model_validate(value)
        except ImportError:
            pass

        # Check if it's a dataclass
        if dataclasses.is_dataclass(target_type) and isinstance(target_type, type):
            if isinstance(value, dict):
                return target_type(**value)

        # For simple types, try direct conversion
        if isinstance(target_type, type):
            try:
                return target_type(value)
            except (TypeError, ValueError):
                pass

        return value
