"""
HTTP Connection Implementation for SurrealDB SDK.

Provides stateless HTTP-based connection, ideal for microservices and serverless.
"""

import asyncio
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal, Self

import httpx

from .base import BaseSurrealConnection

if TYPE_CHECKING:
    from ..transaction import HTTPTransaction
from ..exceptions import ConnectionError, QueryError
from ..protocol.rpc import RPCMethod, RPCRequest, RPCResponse
from ..types import AuthResponse
from ..utils import unescape_record_id

logger = logging.getLogger(__name__)

# Backoff delays (seconds) between attempts when recovering from a transient
# ``401`` caused by a freshly-minted JWT whose ``nbf`` claim is momentarily in
# the future under host clock skew (e.g. WSL2 backward clock jumps). Each attempt
# re-mints the token, so these delays just space out attempts while the clock
# settles; the number of entries bounds how long genuine auth failures take to
# surface.
_AUTH_RETRY_BACKOFFS_S: tuple[float, ...] = (0.1, 0.25, 0.5, 1.0)

# Set, for the duration of the ``signin()`` call, by the transient-401 re-mint
# in ``_post_rpc``: the connection's auth generation when the rejected request
# was sent. ``signin()`` installs the re-minted token only if the generation is
# still that one; otherwise another task changed the auth state meanwhile and
# the result is discarded. Task-local, so a concurrent ``signin()`` from another
# task is unaffected.
_REMINT_FOR_GENERATION: ContextVar[int | None] = ContextVar("_REMINT_FOR_GENERATION", default=None)


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
        # Arguments of the ``signin()`` the current token came from, retained so
        # a transient auth failure (see ``_post_rpc``) can transparently re-mint
        # a token. ``None`` whenever the current identity did not come from a
        # successful ``signin()``: after ``authenticate()``, ``signup()``,
        # ``invalidate()``, ``close()`` or a failed ``signin()``.
        self._signin_args: dict[str, Any] | None = None
        # What still uses this connection, so that ``_drain_and_close`` does
        # not close a client being retired under it: RPC requests in flight
        # through ``_send_rpc``, and open ``HTTPTransaction``s (which send their
        # statements only at commit), counted per task that opened them.
        # ``_released`` is set each time one of them ends.
        self._inflight_rpcs = 0
        self._open_transactions: dict[asyncio.Task[Any] | None, int] = {}
        self._released = asyncio.Event()
        # Set by ``_drain_and_close`` when the task retiring this connection has
        # transactions open on it: the close then happens when they end.
        self._close_after_transactions_of: asyncio.Task[Any] | None = None
        self._close_pending = False
        # Set by ``_close_when_idle`` (a cancelled drain): the last request or
        # transaction to end closes the connection.
        self._close_when_idle_pending = False

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
        """Close HTTP client.

        The connection reads as closed afterwards even when closing the
        underlying ``httpx`` client raises (for example ``RuntimeError: Event
        loop is closed`` when it was bound to a loop that has since been
        closed); the exception still propagates.
        """
        client, self._client = self._client, None
        self._connected = False
        self._authenticated = False
        self._signin_args = None
        self._auth_changed()
        self._close_pending = False
        self._close_after_transactions_of = None
        self._close_when_idle_pending = False
        if client:
            await client.aclose()

    def _forget_signin_credentials(self) -> None:
        """Stop the transient-401 recovery from replaying the last ``signin()``."""
        self._signin_args = None

    async def _drain_and_close(self, timeout: float) -> bool:
        """Close this connection once nothing is using it any more.

        Used to retire a client that has just been replaced: RPC requests
        already running on it, and transactions open on it (an
        ``HTTPTransaction`` sends its statements only at commit), finish
        normally instead of failing when the ``httpx`` client is closed under
        them. The wait is bounded; whatever still uses the connection after
        *timeout* seconds is cut off by the close. Nothing is left running in
        the background: the wait happens in the caller's task.

        Transactions opened by the calling task itself are not waited for, as
        they cannot end while this task waits: the connection is then closed
        when the last of them commits or rolls back, instead of here.

        If the calling task is cancelled during the wait, the connection is
        not closed under what still uses it: it is closed by the last request
        or transaction to end (see ``_close_when_idle``), and the cancellation
        propagates.

        Args:
            timeout: Maximum number of seconds to wait for in-flight requests
                and open transactions of other tasks.

        Returns:
            ``True`` if everything else using the connection finished before
            the close, ``False`` if the timeout expired first.
        """
        me = asyncio.current_task()
        drained = True
        try:
            async with asyncio.timeout(timeout):
                # Clear, then re-check: a request can start between the event
                # being set and this task resuming. Nothing can run between the
                # check and the wait, so no release is missed.
                while True:
                    self._released.clear()
                    if not self._in_use_by_others(me):
                        break
                    await self._released.wait()
        except TimeoutError:
            drained = False
        except asyncio.CancelledError:
            # Only an outside cancellation gets here: the timeout's own is
            # turned into ``TimeoutError`` above.
            await self._close_when_idle()
            raise

        if self._open_transactions.get(me):
            self._close_after_transactions_of = me
            self._close_pending = True
        else:
            await self.close()
        return drained

    async def _close_when_idle(self) -> None:
        """Close this connection now if nothing uses it, otherwise when the last user is done.

        The close is then performed by the last RPC request to return (in
        ``_send_rpc``) or the last transaction to commit or roll back (in
        ``_transaction_ended``), so no task is left waiting for it. Used when
        retiring the connection is cancelled: what still uses it finishes
        normally, and the connection does not stay open for good. A failure to
        close is logged, not raised. Does nothing on a closed connection.
        """
        if self._client is None:
            return
        if self._in_use():
            self._close_when_idle_pending = True
        else:
            await self._close_quietly()

    def _in_use(self) -> bool:
        """Whether an RPC request or an open transaction uses this connection."""
        return bool(self._inflight_rpcs or self._open_transactions)

    async def _close_quietly(self) -> None:
        """Close this connection, logging a failure instead of raising it."""
        try:
            await self.close()
        except Exception:
            logger.warning("Failed to close a retired HTTP connection cleanly.", exc_info=True)

    def _in_use_by_others(self, task: asyncio.Task[Any] | None) -> bool:
        """Whether an RPC request, or a transaction opened by another task than *task*, uses this connection."""
        return bool(self._inflight_rpcs) or any(owner is not task for owner in self._open_transactions)

    def _transaction_opened(self) -> asyncio.Task[Any] | None:
        """Count a transaction opened by the current task as using this connection.

        Called by ``HTTPTransaction`` when it begins. Returns the owning task,
        which the transaction hands back to ``_transaction_ended``.
        """
        owner = asyncio.current_task()
        self._open_transactions[owner] = self._open_transactions.get(owner, 0) + 1
        return owner

    async def _transaction_ended(self, owner: asyncio.Task[Any] | None) -> None:
        """Stop counting a transaction of *owner* that committed or rolled back.

        Performs the close that ``_drain_and_close`` deferred if this was the
        last open transaction of the task that retired this connection, or the
        last use of a connection marked by ``_close_when_idle``. A failure to
        close is logged, not raised: the transaction itself has already ended.
        """
        remaining = self._open_transactions.get(owner, 0) - 1
        if remaining > 0:
            self._open_transactions[owner] = remaining
        else:
            self._open_transactions.pop(owner, None)
        self._released.set()

        retiring_task_done = self._close_pending and remaining <= 0 and owner is self._close_after_transactions_of
        if retiring_task_done or (self._close_when_idle_pending and not self._in_use()):
            await self._close_quietly()

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

        The request counts as in flight until it returns or raises, so
        ``_drain_and_close`` does not close the client under it. If it was the
        last use of a connection marked by ``_close_when_idle``, the
        connection is closed before returning (a failure to close is logged,
        not raised).
        """
        if not self._client:
            raise ConnectionError("Not connected. Call connect() first.")

        self._inflight_rpcs += 1
        try:
            return await self._post_rpc(self._client, request)
        finally:
            self._inflight_rpcs -= 1
            self._released.set()
            if self._close_when_idle_pending and not self._in_use():
                await self._close_quietly()

    async def _post_rpc(self, client: httpx.AsyncClient, request: RPCRequest) -> RPCResponse:
        """POST *request* to ``/rpc`` on *client*, re-minting the token on a transient 401."""
        request.id = self._next_request_id()

        # A freshly-minted SurrealDB JWT carries ``iat == nbf == now``. If the
        # server clock moves *backwards* between minting the token and verifying
        # it on a subsequent request, the token's ``nbf`` claim is momentarily in
        # the future and the server rejects it with a transient ``401`` ("Token
        # verification failed due to the 'nbf' claim containing a future time").
        # This happens on hosts with unstable clocks — most notably WSL2, whose
        # VM clock can jump backwards — and surfaces as non-deterministic 401s
        # under load. We recover by re-minting the token (a fresh signin produces
        # an ``nbf`` aligned with the *current* clock, so it is valid regardless
        # of the jump magnitude) and retrying with a short backoff. Re-minting is
        # only possible when the current token came from a ``signin()`` whose
        # arguments are retained; otherwise (no signin, or a token installed by
        # ``authenticate()`` / ``signup()``) a 401 is treated as a genuine
        # credentials problem and raised immediately.
        #
        # The connection may be shared between tasks. If another task re-minted
        # the token of the same ``signin()`` after this request was sent, the
        # request is retried with that token, without signing in again. If the
        # connection now acts as someone else (another task called
        # ``signin()``, ``authenticate()``, ``signup()``, ``invalidate()`` or
        # ``close()``), the request is never replayed: the 401 is raised. A
        # re-mint started here installs its token only if the auth state is
        # still the one the request was sent with (see ``signin()``).
        last_status_error: httpx.HTTPStatusError | None = None
        for attempt in range(len(_AUTH_RETRY_BACKOFFS_S) + 1):
            # Read together with the headers below, with no await in between.
            sent_token = self._token
            sent_generation = self._auth_generation
            sent_identity = self._auth_identity
            try:
                if self.protocol == "cbor":
                    # Use CBOR encoding which properly handles all data types
                    headers = {
                        **self.headers,
                        "Content-Type": "application/cbor",
                        "Accept": "application/cbor",
                    }
                    response = await client.post(
                        "/rpc",
                        content=request.to_cbor(),
                        headers=headers,
                    )
                    response.raise_for_status()
                    return RPCResponse.from_cbor(response.content)
                else:
                    # Use JSON encoding
                    headers = {**self.headers, "Content-Type": "application/json"}
                    response = await client.post(
                        "/rpc",
                        content=request.to_json(),
                        headers=headers,
                    )
                    response.raise_for_status()
                    return RPCResponse.from_dict(response.json())

            except httpx.HTTPStatusError as e:
                last_status_error = e
                if (
                    e.response.status_code == 401
                    and attempt < len(_AUTH_RETRY_BACKOFFS_S)
                    and self._auth_identity == sent_identity
                ):
                    if self._auth_generation != sent_generation or self._token != sent_token:
                        # Another task renewed the token of the same signin()
                        # since this request was sent: retry with it.
                        continue
                    signin_args = self._signin_args
                    if signin_args is not None:
                        await asyncio.sleep(_AUTH_RETRY_BACKOFFS_S[attempt])
                        remint = _REMINT_FOR_GENERATION.set(sent_generation)
                        try:
                            # Re-mint a token aligned with the current server clock.
                            await self.signin(**signin_args)
                        except Exception:
                            # Best-effort: if re-auth fails, retry with the existing
                            # token anyway (the skew window may simply have passed).
                            # The failed signin dropped the retained arguments, so
                            # that retry is the last one.
                            pass
                        finally:
                            _REMINT_FOR_GENERATION.reset(remint)
                        if self._auth_identity == sent_identity:
                            continue
                        # Another task changed the identity during the re-mint
                        # (whose token was then discarded): do not replay.
                raise QueryError(
                    message=f"HTTP error: {e.response.status_code} - {e.response.text}",
                    code=e.response.status_code,
                )
            except httpx.RequestError as e:
                raise ConnectionError(f"Request failed: {e}")

        # Unreachable: the loop always returns or raises, but keeps type checkers
        # happy about the function always producing a value.
        raise QueryError(  # pragma: no cover
            message=f"HTTP error: {last_status_error.response.status_code if last_status_error else 401}",
            code=last_status_error.response.status_code if last_status_error else 401,
        )

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

        On success the arguments are retained to re-mint the token on a
        transient ``401`` (see ``_post_rpc``). On failure the previously
        retained arguments are dropped, so a later ``401`` is raised rather
        than answered by signing in again as an earlier identity.

        When called by the transient-401 re-mint, the token is installed only
        if the auth state has not changed since the rejected request was sent;
        otherwise the response is returned and the connection left as it is.
        """
        remint_for = _REMINT_FOR_GENERATION.get()
        try:
            response = await self._fetch_signin(user, password, namespace, database, access, **credentials)
        except BaseException:
            if remint_for is None or remint_for == self._auth_generation:
                self._signin_args = None
                self._auth_changed(same_identity=remint_for is not None)
            raise
        if remint_for is not None and remint_for != self._auth_generation:
            # Another task changed the auth state while this re-mint was
            # pending: installing it would undo that change.
            return response
        self._token = response.token
        self._authenticated = True
        # Retain credentials so a transient auth failure can re-mint a token
        # (see the clock-skew handling in ``_post_rpc``).
        self._signin_args = {
            "user": user,
            "password": password,
            "namespace": namespace,
            "database": database,
            "access": access,
            **credentials,
        }
        # A re-mint renews the token of the same arguments; any other signin()
        # may be a different principal.
        self._auth_changed(same_identity=remint_for is not None)
        return response

    async def _fetch_signin(
        self,
        user: str | None,
        password: str | None,
        namespace: str | None,
        database: str | None,
        access: str | None,
        **credentials: Any,
    ) -> AuthResponse:
        """POST to ``/signin`` and return the token, without installing it; see ``signin()``."""
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
            token_data = data.get("token")
            refresh_token: str | None = None

            if isinstance(token_data, dict):
                # SurrealDB 3.0 WITH REFRESH: {"token": {"access": "JWT", "refresh": "..."}}
                token = token_data.get("access") or token_data.get("token")
                refresh_token = token_data.get("refresh")
            elif isinstance(token_data, str):
                # SurrealDB 2.x / 3.x without refresh: {"token": "JWT"}
                token = token_data
            else:
                token = None

            return AuthResponse(token=token, refresh_token=refresh_token, success=True, raw=data)

        except httpx.RequestError as e:
            raise AuthenticationError(f"Authentication request failed: {e}")

    async def invalidate(self) -> None:
        """
        Return the connection to an unauthenticated state.

        Sends the ``invalidate`` RPC with the current token, then clears the
        token, the authenticated flag and the credentials retained by
        ``signin()``. The local state is cleared even when the RPC fails (for
        example with a ``401`` on an expired token), and that error is raised.
        Subsequent requests carry no credentials until the next ``signin()`` or
        ``authenticate()``.

        Raises:
            QueryError: If the server rejects the ``invalidate`` RPC.
            ConnectionError: If the request cannot be sent.
        """
        # Dropped before the RPC so that a 401 on it is not answered by
        # signing in again.
        self._signin_args = None
        try:
            if self._client is not None:
                await self.rpc(RPCMethod.INVALIDATE)
        finally:
            self._token = None
            self._authenticated = False
            self._signin_args = None
            self._auth_changed()

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
            return RecordId(table=table, id=unescape_record_id(id_part))
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

    async def patch(self, thing: str, patches: list[dict[str, Any]]) -> Any:
        """Apply JSON Patch operations with CBOR-aware thing conversion."""
        from ..types import RecordsResponse

        result = await self.rpc("patch", [self._to_thing(thing), patches])
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
