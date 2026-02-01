"""
Base Connection Interface for SurrealDB SDK.

Defines the abstract interface that all connection types must implement.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Self

from ..protocol.rpc import RPCRequest, RPCResponse

if TYPE_CHECKING:
    from ..functions import FunctionNamespace
    from ..transaction import BaseTransaction
from ..types import (
    QueryResponse,
    RecordResponse,
    RecordsResponse,
    AuthResponse,
    InfoResponse,
    DeleteResponse,
)


class BaseSurrealConnection(ABC):
    """
    Abstract base class for SurrealDB connections.

    All connection implementations (HTTP, WebSocket) must inherit from this class.
    """

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
    async def connect(self) -> None:
        """Establish connection to SurrealDB."""
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
        from ..exceptions import QueryError

        request = RPCRequest(method=method, params=params or [])
        response = await self._send_rpc(request)

        if response.is_error:
            raise QueryError(
                message=response.error.message if response.error else "Unknown error",
                code=response.error.code if response.error else None,
            )

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
        response = AuthResponse.from_rpc_result(result)
        if response.token:
            self._token = response.token
        self._authenticated = response.success
        return response

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
            QueryResponse containing results for each statement
        """
        result = await self.rpc("query", [sql, vars or {}])
        return QueryResponse.from_rpc_result(result)

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
