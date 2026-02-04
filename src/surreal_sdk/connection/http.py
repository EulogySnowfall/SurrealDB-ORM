"""
HTTP Connection Implementation for SurrealDB SDK.

Provides stateless HTTP-based connection, ideal for microservices and serverless.
"""

from typing import TYPE_CHECKING, Any, Literal, Self

import httpx

from .base import BaseSurrealConnection

if TYPE_CHECKING:
    from ..transaction import HTTPTransaction
from ..protocol.rpc import RPCRequest, RPCResponse
from ..types import AuthResponse
from ..exceptions import ConnectionError, QueryError


class HTTPConnection(BaseSurrealConnection):
    """
    HTTP-based connection to SurrealDB.

    This connection is stateless - each request is independent.
    Ideal for microservices, serverless, and horizontally scaled applications.

    Authentication is performed via headers on each request.

    Supports both JSON and CBOR protocols. CBOR is recommended as it properly
    handles binary data and avoids string interpretation issues (e.g., 'data:xxx'
    being interpreted as record links).
    """

    def __init__(
        self,
        url: str,
        namespace: str,
        database: str,
        timeout: float = 30.0,
        protocol: Literal["json", "cbor"] = "cbor",
    ):
        """
        Initialize HTTP connection.

        Args:
            url: SurrealDB HTTP URL (e.g., "http://localhost:8000")
            namespace: Target namespace
            database: Target database
            timeout: Request timeout in seconds
            protocol: Serialization protocol ("json" or "cbor").
                      Defaults to "cbor" which properly handles string values
                      that might be misinterpreted as record links (e.g., "data:...").
        """
        # Normalize URL to HTTP if needed
        if url.startswith("ws://"):
            url = url.replace("ws://", "http://", 1)
        elif url.startswith("wss://"):
            url = url.replace("wss://", "https://", 1)

        if protocol not in ("json", "cbor"):
            raise ValueError(f"Invalid protocol '{protocol}'. Must be 'json' or 'cbor'.")

        super().__init__(url, namespace, database, timeout)
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0
        self.protocol: Literal["json", "cbor"] = protocol

    @property
    def headers(self) -> dict[str, str]:
        """Build request headers based on protocol setting."""
        if self.protocol == "cbor":
            content_type = "application/cbor"
            accept = "application/cbor"
        else:
            content_type = "application/json"
            accept = "application/json"

        h = {
            "Surreal-NS": self.namespace,
            "Surreal-DB": self.database,
            "Accept": accept,
            "Content-Type": content_type,
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    @property
    def _json_headers(self) -> dict[str, str]:
        """Build JSON headers for endpoints that always use JSON (sql, rest_*)."""
        h = {
            "Surreal-NS": self.namespace,
            "Surreal-DB": self.database,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _next_request_id(self) -> int:
        """Generate next request ID."""
        self._request_id += 1
        return self._request_id

    async def connect(self) -> Self:
        """Establish HTTP client connection. Returns self for fluent API."""
        if self._connected:
            return self

        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=self.timeout,
            # Disable connection pooling to avoid event loop binding issues in tests
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=100),
        )
        self._connected = True
        return self

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._connected = False
        self._authenticated = False

    async def _send_rpc(self, request: RPCRequest) -> RPCResponse:
        """
        Send RPC request via HTTP POST to /rpc endpoint.

        Uses CBOR or JSON encoding based on the protocol setting.
        CBOR is recommended as it properly handles string values that
        might be misinterpreted as record links (e.g., "data:...").

        Args:
            request: The RPC request to send

        Returns:
            The RPC response

        Raises:
            ConnectionError: If not connected
            QueryError: If request fails
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        request.id = self._next_request_id()

        try:
            if self.protocol == "cbor":
                # Use CBOR encoding which properly handles all data types
                headers = {
                    **self.headers,
                    "Content-Type": "application/cbor",
                    "Accept": "application/cbor",
                }
                response = await self._client.post(
                    "/rpc",
                    content=request.to_cbor(),
                    headers=headers,
                )
                response.raise_for_status()
                return RPCResponse.from_cbor(response.content)
            else:
                # Use JSON encoding
                headers = {**self.headers, "Content-Type": "application/json"}
                response = await self._client.post(
                    "/rpc",
                    content=request.to_json(),
                    headers=headers,
                )
                response.raise_for_status()
                return RPCResponse.from_dict(response.json())

        except httpx.HTTPStatusError as e:
            raise QueryError(
                message=f"HTTP error: {e.response.status_code} - {e.response.text}",
                code=e.response.status_code,
            )
        except httpx.RequestError as e:
            raise ConnectionError(f"Request failed: {e}")

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
        Authenticate with SurrealDB via HTTP.

        For HTTP connections, this obtains a JWT token that will be
        included in subsequent request headers.

        Args:
            user: Username (for root/namespace/database auth)
            password: Password (for root/namespace/database auth)
            namespace: Optional namespace scope
            database: Optional database scope
            access: Optional access method (for record access auth)
            **credentials: Additional credentials for record access (email, password, etc.)
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        from ..exceptions import AuthenticationError

        payload: dict[str, Any] = {}
        if user:
            payload["user"] = user
        if namespace:
            payload["ns"] = namespace
        if database:
            payload["db"] = database
        if access:
            # Record access auth: password goes as 'password' in credentials
            payload["ac"] = access
            if password:
                payload["password"] = password
        else:
            # Root/namespace/database auth: password goes as 'pass'
            if password:
                payload["pass"] = password
        # Add any additional credentials for record access
        payload.update(credentials)

        try:
            response = await self._client.post(
                "/signin",
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )

            if response.status_code != 200:
                raise AuthenticationError(f"Authentication failed: {response.text}")

            data = response.json()
            token = data.get("token")
            self._token = token
            self._authenticated = True
            return AuthResponse(token=token, success=True, raw=data)

        except httpx.RequestError as e:
            raise AuthenticationError(f"Authentication request failed: {e}")

    async def sql(self, query: str, vars: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """
        Execute raw SurrealQL via POST /sql endpoint.

        This is a direct SQL execution endpoint, alternative to RPC.
        Always uses JSON protocol regardless of connection protocol setting.

        Args:
            query: SurrealQL query string
            vars: Query variables (passed as query params)

        Returns:
            Query results
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        try:
            # Always use JSON headers for /sql endpoint (plain text query)
            response = await self._client.post(
                "/sql",
                content=query,
                headers=self._json_headers,
                params=vars,
            )
            response.raise_for_status()
            result: list[dict[str, Any]] = response.json()
            return result

        except httpx.HTTPStatusError as e:
            raise QueryError(
                message=f"SQL query failed: {e.response.text}",
                query=query,
                code=e.response.status_code,
            )

    async def health(self) -> bool:
        """
        Check server health via GET /health endpoint.

        Returns:
            True if server is healthy
        """
        if not self._client:
            return False

        try:
            response = await self._client.get("/health")
            return response.status_code == 200
        except Exception:
            return False  # Any error means server is unhealthy

    async def status(self) -> bool:
        """
        Check server status via GET /status endpoint.

        Returns:
            True if server is running
        """
        if not self._client:
            return False

        try:
            response = await self._client.get("/status")
            return response.status_code == 200
        except Exception:
            return False  # Any error means server is not running

    # REST-style CRUD endpoints (alternative to RPC)

    async def rest_select(self, table: str, record_id: str | None = None) -> list[dict[str, Any]]:
        """
        Select via REST GET /key/:table or /key/:table/:id.

        Args:
            table: Table name
            record_id: Optional record ID

        Returns:
            Records
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        path = f"/key/{table}"
        if record_id:
            path += f"/{record_id}"

        response = await self._client.get(path, headers=self._json_headers)
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, list) else [result]

    async def rest_create(
        self,
        table: str,
        data: dict[str, Any],
        record_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Create via REST POST /key/:table or /key/:table/:id.

        Args:
            table: Table name
            data: Record data
            record_id: Optional record ID

        Returns:
            Created record
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        path = f"/key/{table}"
        if record_id:
            path += f"/{record_id}"

        response = await self._client.post(path, json=data, headers=self._json_headers)
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def rest_update(
        self,
        table: str,
        record_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Update via REST PUT /key/:table/:id.

        Args:
            table: Table name
            record_id: Record ID
            data: New record data

        Returns:
            Updated record
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        response = await self._client.put(
            f"/key/{table}/{record_id}",
            json=data,
            headers=self._json_headers,
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def rest_patch(
        self,
        table: str,
        record_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Patch via REST PATCH /key/:table/:id.

        Args:
            table: Table name
            record_id: Record ID
            data: Fields to update

        Returns:
            Updated record
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        response = await self._client.patch(
            f"/key/{table}/{record_id}",
            json=data,
            headers=self._json_headers,
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    async def rest_delete(
        self,
        table: str,
        record_id: str | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """
        Delete via REST DELETE /key/:table or /key/:table/:id.

        Args:
            table: Table name
            record_id: Optional record ID

        Returns:
            Deleted record(s)
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        path = f"/key/{table}"
        if record_id:
            path += f"/{record_id}"

        response = await self._client.delete(path, headers=self._json_headers)
        response.raise_for_status()
        result: dict[str, Any] | list[dict[str, Any]] = response.json()
        return result

    # Transaction support

    def transaction(self) -> "HTTPTransaction":
        """
        Create a new HTTP transaction.

        HTTP transactions batch all statements and execute them atomically on commit.

        Usage:
            async with conn.transaction() as tx:
                await tx.create("users", {"name": "Alice"})
                await tx.create("orders", {"user": "users:alice"})
                # All statements executed atomically on exit

        Returns:
            HTTPTransaction context manager
        """
        from ..transaction import HTTPTransaction

        return HTTPTransaction(self)

    def _to_thing(self, thing: str) -> Any:
        """
        Convert a thing string to the appropriate type for the protocol.

        For CBOR protocol, converts "table:id" strings to RecordId objects.
        For JSON protocol, returns the string as-is.

        Handles backtick-escaped IDs (e.g., "table:`7abc`" -> RecordId(table, "7abc")).

        Args:
            thing: Thing reference (table name or "table:id")

        Returns:
            RecordId object for CBOR if thing contains ":", else string
        """
        if self.protocol == "cbor" and ":" in thing:
            from ..protocol.cbor import RecordId

            table, id_part = thing.split(":", 1)

            # Handle backtick-escaped IDs (e.g., "`7abc`" -> "7abc")
            if id_part.startswith("`") and id_part.endswith("`"):
                id_part = id_part[1:-1].replace("``", "`")

            return RecordId(table=table, id=id_part)
        return thing

    async def select(self, thing: str) -> Any:
        """Select records with CBOR-aware thing conversion."""
        from ..types import RecordsResponse

        result = await self.rpc("select", [self._to_thing(thing)])
        return RecordsResponse.from_rpc_result(result)

    async def create(self, thing: str, data: dict[str, Any] | None = None) -> Any:
        """Create a record with CBOR-aware thing conversion."""
        from ..types import RecordResponse

        params: list[Any] = [self._to_thing(thing)]
        if data:
            params.append(data)
        result = await self.rpc("create", params)
        return RecordResponse.from_rpc_result(result)

    async def upsert(self, thing: str | Any, data: dict[str, Any] | None = None) -> Any:
        """Upsert a record with CBOR-aware thing conversion."""
        from ..types import RecordsResponse

        # Handle both string and RecordId objects
        if isinstance(thing, str):
            thing_param = self._to_thing(thing)
        else:
            thing_param = thing

        params: list[Any] = [thing_param]
        if data:
            params.append(data)
        result = await self.rpc("upsert", params)
        return RecordsResponse.from_rpc_result(result)

    async def update(self, thing: str, data: dict[str, Any] | None = None) -> Any:
        """Update a record with CBOR-aware thing conversion."""
        from ..types import RecordsResponse

        params: list[Any] = [self._to_thing(thing)]
        if data:
            params.append(data)
        result = await self.rpc("update", params)
        return RecordsResponse.from_rpc_result(result)

    async def merge(self, thing: str, data: dict[str, Any]) -> Any:
        """Merge a record with CBOR-aware thing conversion."""
        from ..types import RecordsResponse

        result = await self.rpc("merge", [self._to_thing(thing), data])
        return RecordsResponse.from_rpc_result(result)

    async def delete(self, thing: str) -> Any:
        """Delete a record with CBOR-aware thing conversion."""
        from ..types import DeleteResponse

        result = await self.rpc("delete", [self._to_thing(thing)])
        return DeleteResponse.from_rpc_result(result)

    async def relate(
        self,
        from_thing: str,
        relation: str,
        to_thing: str,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """
        Create a graph relationship between records.

        For CBOR protocol, this converts string record IDs to RecordId objects
        which SurrealDB requires for the relate operation.
        """
        from ..types import RecordResponse

        params: list[Any] = [self._to_thing(from_thing), relation, self._to_thing(to_thing)]
        if data:
            params.append(data)

        result = await self.rpc("relate", params)
        return RecordResponse.from_rpc_result(result)
