"""
HTTP Connection Implementation for SurrealDB SDK.

Provides stateless HTTP-based connection, ideal for microservices and serverless.
"""

from typing import TYPE_CHECKING, Any, Self

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
    """

    def __init__(
        self,
        url: str,
        namespace: str,
        database: str,
        timeout: float = 30.0,
    ):
        """
        Initialize HTTP connection.

        Args:
            url: SurrealDB HTTP URL (e.g., "http://localhost:8000")
            namespace: Target namespace
            database: Target database
            timeout: Request timeout in seconds
        """
        # Normalize URL to HTTP if needed
        if url.startswith("ws://"):
            url = url.replace("ws://", "http://", 1)
        elif url.startswith("wss://"):
            url = url.replace("wss://", "https://", 1)

        super().__init__(url, namespace, database, timeout)
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0

    @property
    def headers(self) -> dict[str, str]:
        """Build request headers."""
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
            # Use pre-encoded JSON with custom encoder for datetime, UUID, etc.
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

        Args:
            query: SurrealQL query string
            vars: Query variables (passed as query params)

        Returns:
            Query results
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        try:
            response = await self._client.post(
                "/sql",
                content=query,
                headers=self.headers,
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
            return False

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
            return False

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

        response = await self._client.get(path, headers=self.headers)
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

        response = await self._client.post(path, json=data, headers=self.headers)
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
            headers=self.headers,
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
            headers=self.headers,
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

        response = await self._client.delete(path, headers=self.headers)
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
