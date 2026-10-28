"""
Authentication mixins for SurrealDB ORM models.

Provides signup/signin methods for USER type models using
SurrealDB's native JWT authentication.
"""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING, Any, ClassVar, Self

from surreal_sdk.exceptions import AuthenticationError, QueryError, SurrealDBError

from .result import AuthResult

if TYPE_CHECKING:
    from surreal_sdk import HTTPConnection


class AuthenticatedUserMixin:
    """
    Mixin providing authentication methods for User models.

    Add this mixin to your USER type models to enable signup/signin
    functionality using SurrealDB's DEFINE ACCESS authentication.

    Each auth operation (signup, signin, authenticate_token) creates an
    **ephemeral** connection that is closed after use, leaving the root
    singleton connection untouched.

    Token validation results are cached in-memory (TTL-based) to avoid
    creating an ephemeral connection on every request.  Configure via
    :meth:`configure_token_cache`.  An entry never outlives the token's own
    ``exp`` claim, and entries are kept per model table.

    Example:
        class User(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                identifier_field="email",
                password_field="password",
                access_name="account",  # Custom access name (optional)
            )

            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # Create a new user (returns user + JWT token)
        user, token = await User.signup(
            email="test@example.com", password="secret", name="Test"
        )

        # Authenticate existing user
        user, token = await User.signin(
            email="test@example.com", password="secret"
        )

        # Validate a token and get the record ID (cached)
        record_id = await User.validate_token(token)
        # "users:johndoe"

        # Lightweight local validation (no network call)
        record_id = User.validate_token_local(token)
    """

    # --- Token cache (shared across all subclasses) ---
    # Key: (model table, token).  Value: (record ID, monotonic deadline, exp).
    # The table is part of the key because this dict is shared by every
    # subclass: a token validated for one model must never be served to
    # another one.
    _token_cache: ClassVar[dict[tuple[str, str], tuple[str, float, float]]] = {}
    _token_cache_ttl: ClassVar[int] = 300  # seconds
    _token_cache_max_size: ClassVar[int] = 1000  # max cached tokens

    # Stub for mypy — overridden by BaseSurrealModel.get_connection_name()
    @classmethod
    def get_connection_name(cls) -> str:  # pragma: no cover
        return "default"

    @classmethod
    def configure_token_cache(cls, *, ttl: int = 300, max_size: int = 1000) -> None:
        """
        Configure the in-memory token validation cache.

        Args:
            ttl: Time-to-live in seconds for cached entries (default 300).
                 Set to 0 to disable caching.
            max_size: Maximum number of cached tokens (default 1000).
                      Oldest entries are evicted when the limit is reached.
        """
        AuthenticatedUserMixin._token_cache_ttl = ttl
        AuthenticatedUserMixin._token_cache_max_size = max(0, max_size)

    @classmethod
    def invalidate_token_cache(cls, token: str | None = None) -> None:
        """
        Invalidate cached token validation results.

        Args:
            token: Specific token to invalidate (for every model that cached
                   it).  If ``None``, clears the entire cache.
        """
        if token is None:
            cls._token_cache.clear()
        else:
            for key in [key for key in cls._token_cache if key[1] == token]:
                cls._token_cache.pop(key, None)

    @staticmethod
    def _decode_jwt_claims(token: str) -> dict[str, Any] | None:
        """
        Decode the payload of a JWT without verifying its signature.

        Returns:
            The claims object, or ``None`` when the token is not a
            three-part JWT whose payload is a base64url-encoded JSON object.
        """
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None

            # Decode the payload (second part) with padding fix
            payload_b64 = parts[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding

            claims = json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception:
            return None
        return claims if isinstance(claims, dict) else None

    @classmethod
    def _token_expiry(cls, token: str) -> float | None:
        """
        Return the token's ``exp`` claim (seconds since the epoch).

        Returns:
            The expiry, or ``None`` when it cannot be read (opaque token, no
            ``exp`` claim, or a non-numeric one).
        """
        claims = cls._decode_jwt_claims(token)
        if claims is None:
            return None
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, int | float):
            return None
        return float(exp)

    @classmethod
    def _auth_table_name(cls) -> str:
        """Return the table this model's records live in."""
        config = getattr(cls, "model_config", {})
        table_name: str = config.get("table_name") or cls.__name__
        return table_name

    @classmethod
    def _is_own_record(cls, record_id: Any) -> bool:
        """
        Tell whether a ``$auth`` record ID belongs to this model's table.

        A decoded ``RecordId`` (CBOR) is compared by its ``table``
        attribute; a ``"table:id"`` string (JSON) by the part before the
        first ``:``, with SurrealDB's identifier quoting removed.  Anything
        else is not a record of this table.
        """
        table: Any
        if isinstance(record_id, str):
            if ":" not in record_id:
                return False
            table = record_id.split(":", 1)[0]
            if len(table) >= 2 and (table[0], table[-1]) in (("`", "`"), ("\u27e8", "\u27e9")):
                table = table[1:-1]
        else:
            # RecordId (duck-typed: the SDK may be importable under two
            # module paths, each with its own class object).
            table = getattr(record_id, "table", None)
        return isinstance(table, str) and table == cls._auth_table_name()

    @classmethod
    def validate_token_local(cls, token: str) -> str | None:
        """
        Decode a JWT token locally and return the record ID.

        This performs **no network call** and **no cryptographic signature
        verification**.  It simply base64-decodes the JWT payload, extracts
        the record ID (``ID`` claim), and checks the ``exp`` claim for
        expiry.

        Use this in **trusted backend** scenarios where the token was already
        validated at the edge (e.g., API gateway) and you only need the
        record ID for routing/context.

        Args:
            token: JWT token string (header.payload.signature)

        Returns:
            Record ID string (e.g., ``"users:abc123"``) or ``None`` if the
            token is malformed or expired.
        """
        claims = cls._decode_jwt_claims(token)
        if claims is None:
            return None
        try:
            # Check expiry
            exp = claims.get("exp")
            if exp is not None and time.time() > exp:
                return None

            # Extract record ID — SurrealDB uses "ID" claim
            record_id = claims.get("ID") or claims.get("id")
            if record_id is None:
                return None

            return str(record_id)
        except Exception:
            return None

    @classmethod
    async def _create_auth_client(cls) -> HTTPConnection:
        """
        Create an isolated HTTP connection for auth operations.

        Auth operations (signup/signin/authenticate) mutate the connection's
        token and auth state.  Using the root singleton would corrupt it for
        all other concurrent ORM operations.  This method creates a fresh,
        ephemeral connection that callers must close via ``await client.close()``.

        Respects the model's named connection (multi-DB support).
        """
        from surreal_sdk import HTTPConnection

        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        conn_name = cls.get_connection_name()
        config = SurrealDBConnectionManager.get_config(conn_name)

        if config is None:
            raise SurrealDbError(
                f"Connection '{conn_name}' not configured. "
                "Call SurrealDBConnectionManager.set_connection() or add_connection() first."
            )

        client = HTTPConnection(
            config.url,
            config.namespace,
            config.database,
            protocol=config.protocol,
        )
        await client.connect()
        return client

    @classmethod
    async def define_access(
        cls,
        *,
        access_name: str | None = None,
        token_duration: str | None = None,
        session_duration: str | None = None,
        grant_duration: str | None = None,
        with_refresh: bool | None = None,
        identifier_field: str | None = None,
        password_field: str | None = None,
    ) -> str:
        """
        Generate and apply the DEFINE ACCESS statement for this model.

        Inspects the model's config and executes the corresponding
        ``DEFINE ACCESS`` statement against the database.  Any keyword
        argument overrides the value from ``SurrealConfigDict``.

        Only available on ``TableType.USER`` models that use
        :class:`AuthenticatedUserMixin`.

        Args:
            access_name: Override the access definition name.
            token_duration: Override JWT token lifetime (e.g., ``"1h"``).
            session_duration: Override session lifetime (e.g., ``"24h"``).
            grant_duration: Override refresh token lifetime (e.g., ``"30d"``).
                Only relevant when *with_refresh* is ``True``.
            with_refresh: Override whether to enable refresh tokens.
            identifier_field: Override the field used for signin
                (e.g., ``"username"`` instead of ``"email"``).
            password_field: Override the password field name.

        Returns:
            The SurrealQL statement that was executed.

        Raises:
            TypeError: If the model is not a USER type table.

        Example::

            # Using model config defaults
            await User.define_access()

            # Overriding at call site
            await User.define_access(
                token_duration="1h",
                session_duration="24h",
                with_refresh=True,
                grant_duration="14d",
            )
        """
        from .access import AccessGenerator

        access_def = AccessGenerator.from_model(cls)  # type: ignore[arg-type]
        if access_def is None:
            raise TypeError(
                f"{cls.__name__} is not a USER type table. define_access() is only available on TableType.USER models."
            )

        # Apply overrides
        if access_name is not None:
            access_def.name = access_name
        if token_duration is not None:
            access_def.duration_token = token_duration
        if session_duration is not None:
            access_def.duration_session = session_duration
        if grant_duration is not None:
            access_def.duration_grant = grant_duration
        if with_refresh is not None:
            access_def.with_refresh = with_refresh
        if identifier_field is not None:
            access_def.identifier_field = identifier_field
            # Rebuild signin_where with the new identifier field
            access_def.signin_where = (
                f"{identifier_field} = ${identifier_field} AND "
                f"crypto::{access_def.algorithm}::compare("
                f"{access_def.password_field}, ${access_def.password_field})"
            )
        if password_field is not None:
            access_def.password_field = password_field
            # Rebuild signin_where with the new password field
            id_field = identifier_field or access_def.identifier_field
            access_def.signin_where = (
                f"{id_field} = ${id_field} AND crypto::{access_def.algorithm}::compare({password_field}, ${password_field})"
            )

        sql = access_def.to_surreal_ql()

        from ..connection_manager import SurrealDBConnectionManager

        conn_name = cls.get_connection_name()
        client = await SurrealDBConnectionManager.get_client(conn_name)
        await client.query(sql)

        return sql

    @staticmethod
    async def _read_session_record(client: HTTPConnection) -> Any:
        """
        Return the ``$auth`` record ID of an authenticated session.

        ``client`` must still be open and carry the token returned by
        signin/signup/authenticate.  The value is returned as decoded (a
        ``RecordId`` under CBOR, a ``"table:id"`` string under JSON) so it can
        be bound back unchanged: stringifying a ``RecordId`` drops the key's
        escaping, and ``type::record("t:000000")`` would read the key as the
        number ``0``.

        Returns:
            The record ID, or ``None`` when the session has no ``$auth`` record.
        """
        auth_result = await client.query("RETURN $auth")
        first_qr = auth_result.first_result
        if first_qr is None:
            return None
        return first_qr.result

    @classmethod
    async def _fetch_record_by_id(cls, record_id: Any) -> Self | None:
        """
        Fetch the user record with this exact record ID via the root connection.

        Args:
            record_id: Record ID as returned by :meth:`_read_session_record`.

        Returns:
            The model instance, or ``None`` when no such record exists.
        """
        from ..connection_manager import SurrealDBConnectionManager

        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__

        root_client = await SurrealDBConnectionManager.get_client(cls.get_connection_name())
        result = await root_client.query(
            f"SELECT * FROM {table_name} WHERE id = type::record($record_id)",
            {"record_id": record_id},
        )
        if result.is_empty:
            return None
        user: Self = cls.from_db(result.first)  # type: ignore[attr-defined]
        return user

    @classmethod
    async def signup(
        cls,
        **credentials: Any,
    ) -> AuthResult[Self]:
        """
        Create a new user via DEFINE ACCESS signup.

        This method uses SurrealDB's native signup functionality, which:
        1. Validates the credentials against the ACCESS definition
        2. Creates the user record with encrypted password
        3. Returns JWT access and refresh tokens (SurrealDB 3.0+)

        An ephemeral connection is used so the root singleton is not affected.

        Args:
            **credentials: User credentials matching the ACCESS definition
                          (e.g., email, password, name, language)

        Returns:
            :class:`AuthResult` containing user instance, access token, and
            refresh token.  Backward-compatible with tuple unpacking::

                user, token = await User.signup(...)          # still works
                result = await User.signup(...)
                result.refresh_token                          # new in 0.30

        The returned ``user`` is the record the session token was issued
        for (``$auth``), fetched by its record ID.

        Raises:
            SurrealDbError: If signup fails (e.g., duplicate identifier), or
                the session record cannot be read after signup
            DoesNotExist: If the session record no longer exists

        Example:
            result = await User.signup(
                email="user@example.com",
                password="secure_password",
                name="John Doe",
            )
            print(result.token, result.refresh_token)
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        # Get access configuration from model
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        access_name = config.get("access_name") or f"{table_name.lower()}_auth"

        # Get connection info from named connection config
        conn_name = cls.get_connection_name()
        conn_config = SurrealDBConnectionManager.get_config(conn_name)
        if conn_config is None:
            raise SurrealDbError(f"Connection '{conn_name}' not configured.")

        namespace = conn_config.namespace
        database = conn_config.database

        if not namespace or not database:
            raise SurrealDbError("Namespace and database must be set for authentication")

        # Use an ephemeral connection — never the root singleton
        client = await cls._create_auth_client()
        try:
            response = await client.signup(
                namespace=namespace,
                database=database,
                access=access_name,
                **credentials,
            )

            if not response.success:
                raise SurrealDbError(f"Signup failed: {response.raw}")

            token = response.token
            if not token:
                raise SurrealDbError("Signup succeeded but no JWT token was returned by the server.")

            refresh_token = response.refresh_token
            # Read $auth while the ephemeral session is still open.
            session_record = await cls._read_session_record(client)
        except (SurrealDBError, AuthenticationError, QueryError) as e:
            raise SurrealDbError(f"Signup failed: {e}") from e
        finally:
            await client.close()

        if session_record is None:
            raise SurrealDbError("Signup succeeded but the session record could not be read ($auth is empty).")

        # Fetch the record the token was issued for, via the root singleton.
        # Never look the user up again by identifier_field: the ACCESS clause
        # already chose the record, and the identifier may not be unique.
        user = await cls._fetch_record_by_id(session_record)
        if user is None:
            raise cls.DoesNotExist(f"User {session_record} not found after signup")  # type: ignore[attr-defined]

        return AuthResult(user=user, token=token, refresh_token=refresh_token)

    @classmethod
    async def _signin_session(cls, **credentials: Any) -> tuple[str, str | None, Any]:
        """
        Sign in on an ephemeral connection and read the session's ``$auth``.

        Shared by :meth:`signin` and :meth:`change_password` so both act on
        the record the ACCESS ``SIGNIN`` clause selected, bound exactly as the
        server returned it (see :meth:`_read_session_record`).

        Returns:
            ``(token, refresh_token, session_record)``.

        Raises:
            SurrealDbError: If signin fails, or ``$auth`` is empty afterwards.
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        # Get access configuration from model
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        access_name = config.get("access_name") or f"{table_name.lower()}_auth"

        # Get connection info from named connection config
        conn_name = cls.get_connection_name()
        conn_config = SurrealDBConnectionManager.get_config(conn_name)
        if conn_config is None:
            raise SurrealDbError(f"Connection '{conn_name}' not configured.")

        namespace = conn_config.namespace
        database = conn_config.database

        if not namespace or not database:
            raise SurrealDbError("Namespace and database must be set for authentication")

        # Use an ephemeral connection — never the root singleton
        client = await cls._create_auth_client()
        try:
            response = await client.signin(
                namespace=namespace,
                database=database,
                access=access_name,
                **credentials,
            )

            if not response.success or not response.token:
                raise SurrealDbError(f"Signin failed: {response.raw}")

            token = response.token
            refresh_token = response.refresh_token
            # Read $auth while the ephemeral session is still open.
            session_record = await cls._read_session_record(client)
        except (SurrealDBError, AuthenticationError, QueryError) as e:
            raise SurrealDbError(f"Signin failed: {e}") from e
        finally:
            await client.close()

        if session_record is None:
            raise SurrealDbError("Signin succeeded but the session record could not be read ($auth is empty).")

        return token, refresh_token, session_record

    @classmethod
    async def signin(
        cls,
        **credentials: Any,
    ) -> AuthResult[Self]:
        """
        Authenticate a user via DEFINE ACCESS signin.

        This method uses SurrealDB's native signin functionality, which:
        1. Validates credentials against the ACCESS definition
        2. Compares the password using the configured algorithm
        3. Returns JWT access and refresh tokens (SurrealDB 3.0+)

        An ephemeral connection is used so the root singleton is not affected.

        Args:
            **credentials: User credentials (identifier and password)

        Returns:
            :class:`AuthResult` containing user instance, access token, and
            refresh token.  Backward-compatible with tuple unpacking::

                user, token = await User.signin(...)          # still works
                result = await User.signin(...)
                result.refresh_token                          # new in 0.30

        The returned ``user`` is the record the session token was issued
        for (``$auth``), fetched by its record ID — the record the ACCESS
        ``SIGNIN`` clause selected, even when the identifier is not unique.

        Raises:
            SurrealDbError: If signin fails (invalid credentials), or the
                session record cannot be read after signin
            DoesNotExist: If the session record no longer exists

        Example:
            result = await User.signin(
                email="user@example.com",
                password="secure_password",
            )
            print(result.token, result.refresh_token)
        """
        token, refresh_token, session_record = await cls._signin_session(**credentials)

        # Fetch the record the token was issued for, via the root singleton.
        # Never look the user up again by identifier_field: the ACCESS clause
        # already chose the record, and the identifier may not be unique.
        user = await cls._fetch_record_by_id(session_record)
        if user is None:
            raise cls.DoesNotExist(f"User {session_record} not found after signin")  # type: ignore[attr-defined]

        return AuthResult(user=user, token=token, refresh_token=refresh_token)

    @classmethod
    async def authenticate_token(
        cls,
        token: str,
    ) -> tuple[Self, str] | None:
        """
        Authenticate using an existing JWT token.

        Validates the token with SurrealDB, retrieves the ``$auth`` record ID,
        and fetches the full user record.

        An ephemeral connection is used so the root singleton is not affected.

        Args:
            token: JWT token from previous signup/signin

        Returns:
            Tuple of (user instance, record_id string) if valid, None otherwise

        Example:
            result = await User.authenticate_token(stored_token)
            if result:
                user, record_id = result
                print(f"Authenticated as {user.email} ({record_id})")
        """

        client = await cls._create_auth_client()
        try:
            # Authenticate with the token on the ephemeral connection
            response = await client.authenticate(token)

            if not response.success:
                return None

            # Get the record ID from $auth (returns a RecordId scalar, not a list)
            auth_result = await client.query("RETURN $auth")
            first_qr = auth_result.first_result
            if first_qr is None or first_qr.result is None:
                return None

            raw_auth = first_qr.result  # RecordId object from CBOR
            if not cls._is_own_record(raw_auth):
                # The token is valid but belongs to another model's table.
                return None
            record_id = str(raw_auth)
        except (SurrealDBError, AuthenticationError, QueryError):
            return None
        finally:
            await client.close()

        # Fetch the full user record via root singleton, binding the decoded
        # record ID rather than its string form (see _read_session_record).
        user = await cls._fetch_record_by_id(raw_auth)
        if user is None:
            return None
        return user, record_id

    @classmethod
    async def validate_token(
        cls,
        token: str,
        *,
        use_cache: bool = True,
    ) -> str | None:
        """
        Validate a JWT token and return the record ID.

        This is a lightweight alternative to :meth:`authenticate_token` that
        only validates the token and returns the ``$auth`` record ID without
        fetching the full user record.

        Results are cached in-memory (default TTL 300 s) to avoid creating an
        ephemeral HTTP connection on every call.  Disable with
        ``use_cache=False`` or ``configure_token_cache(ttl=0)``.  A cached
        entry expires at the token's ``exp`` claim if that comes first; a
        token whose ``exp`` cannot be read is validated on every call and
        never cached.  Entries are kept per model table.

        A token whose ``$auth`` record is not in this model's table returns
        ``None`` (validate it through the model it was issued for).

        An ephemeral connection is used on cache miss so the root singleton
        is not affected.

        Args:
            token: JWT token from previous signup/signin
            use_cache: Whether to use the in-memory cache (default ``True``)

        Returns:
            Record ID string (e.g., ``"users:johndoe"``) if valid and in this
            model's table, None otherwise

        Example:
            record_id = await User.validate_token(token)
            if record_id:
                print(f"Token belongs to {record_id}")
        """
        cache_key = (cls._auth_table_name(), token)

        # --- Cache hit ---
        if use_cache and cls._token_cache_ttl > 0:
            cached = cls._token_cache.get(cache_key)
            if cached is not None:
                record_id, expires_at, cached_exp = cached
                # Both clocks: the monotonic deadline guards against wall-clock
                # jumps, the exp check against a deadline computed from a
                # wall clock that has since moved.
                if time.monotonic() < expires_at and time.time() < cached_exp:
                    return record_id
                # Expired — remove stale entry
                cls._token_cache.pop(cache_key, None)

        # --- Cache miss — ephemeral connection ---
        client = await cls._create_auth_client()
        try:
            response = await client.authenticate(token)

            if not response.success:
                return None

            auth_result = await client.query("RETURN $auth")
            first_qr = auth_result.first_result
            if first_qr is None or first_qr.result is None:
                return None

            if not cls._is_own_record(first_qr.result):
                # The token is valid but belongs to another model's table.
                return None
            record_id = str(first_qr.result)

            # --- Store in cache (with eviction) ---
            # Only a token with a readable exp is cached, and never past it.
            token_exp = cls._token_expiry(token)
            if (
                use_cache
                and cls._token_cache_ttl > 0
                and cls._token_cache_max_size > 0
                and token_exp is not None
                and (remaining := token_exp - time.time()) > 0
            ):
                # Evict oldest entry if cache is full
                if len(cls._token_cache) >= cls._token_cache_max_size and cls._token_cache:
                    oldest_key = next(iter(cls._token_cache))
                    cls._token_cache.pop(oldest_key, None)
                lifetime = min(float(cls._token_cache_ttl), remaining)
                cls._token_cache[cache_key] = (record_id, time.monotonic() + lifetime, token_exp)

            return record_id
        except (SurrealDBError, AuthenticationError, QueryError):
            return None
        finally:
            await client.close()

    @classmethod
    async def refresh_access_token(
        cls,
        refresh_token: str,
    ) -> AuthResult[Self]:
        """
        Exchange a refresh token for a new access token.

        SurrealDB 3.0 issues refresh tokens alongside access tokens during
        signup/signin (when ``WITH REFRESH`` is enabled on the access
        definition).  When the short-lived access token expires, call this
        method with the refresh token to obtain a fresh access token and a
        rotated refresh token without requiring the user to re-enter
        credentials.

        Internally this calls the ``signin`` RPC/endpoint with the
        ``refresh`` parameter — the same mechanism used by the SurrealDB
        HTTP ``/signin`` endpoint.  The old refresh token is revoked
        automatically by the server (token rotation).

        An ephemeral connection is used so the root singleton is not affected.

        Args:
            refresh_token: The refresh token obtained from a previous
                signup/signin or refresh call (prefixed ``surreal-refresh-``).

        Returns:
            :class:`AuthResult` with the user instance, new access token,
            and rotated refresh token.

        Raises:
            SurrealDbError: If the refresh token is invalid or expired.

        Example:
            result = await User.refresh_access_token(stored_refresh_token)
            new_access_token = result.token
            new_refresh_token = result.refresh_token  # rotated — must store
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        # Get access configuration from model
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        access_name = config.get("access_name") or f"{table_name.lower()}_auth"

        # Get connection info from named connection config
        conn_name = cls.get_connection_name()
        conn_config = SurrealDBConnectionManager.get_config(conn_name)
        if conn_config is None:
            raise SurrealDbError(f"Connection '{conn_name}' not configured.")

        namespace = conn_config.namespace
        database = conn_config.database

        if not namespace or not database:
            raise SurrealDbError("Namespace and database must be set for token refresh")

        # Use an ephemeral connection — never the root singleton.
        # SurrealDB 3.0 refresh flow: POST /signin with {"refresh": "..."}
        # Via RPC: signin({ns, db, ac, refresh: "..."})
        client = await cls._create_auth_client()
        try:
            response = await client.signin(
                namespace=namespace,
                database=database,
                access=access_name,
                refresh=refresh_token,
            )

            if not response.success or not response.token:
                raise SurrealDbError(f"Token refresh failed: {response.raw}")

            new_token = response.token
            new_refresh = response.refresh_token
        except (SurrealDBError, AuthenticationError, QueryError) as e:
            raise SurrealDbError(f"Token refresh failed: {e}") from e
        finally:
            await client.close()

        # Authenticate on the ephemeral connection to get $auth record ID,
        # then fetch the full user record via root singleton.
        # Instead, we decode the JWT locally to extract the record ID —
        # the token was just issued by the server so it's trustworthy.
        record_id = cls.validate_token_local(new_token)
        if not record_id:
            raise SurrealDbError("Token refresh succeeded but could not extract record ID from JWT.")

        root_client = await SurrealDBConnectionManager.get_client(conn_name)
        result = await root_client.query(
            f"SELECT * FROM {table_name} WHERE id = type::record($record_id)",
            {"record_id": record_id},
        )

        if result.is_empty:
            raise cls.DoesNotExist("User not found after token refresh")  # type: ignore[attr-defined]

        user = cls.from_db(result.first)  # type: ignore[attr-defined]
        return AuthResult(user=user, token=new_token, refresh_token=new_refresh)

    @classmethod
    async def change_password(
        cls,
        identifier_value: str,
        old_password: str,
        new_password: str,
    ) -> bool:
        """
        Change a user's password.

        Validates the old password by attempting signin (uses ephemeral
        connection), then updates the password of the record that signin
        authenticated, via the root singleton.

        Args:
            identifier_value: Value of the identifier field (e.g., email)
            old_password: Current password for verification
            new_password: New password to set

        Returns:
            True if password was changed successfully

        Raises:
            SurrealDbError: If old password is incorrect, or the update did
                not change exactly the signed-in record
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        # First verify the old password by attempting signin
        config = getattr(cls, "model_config", {})
        identifier_field = config.get("identifier_field", "email")
        password_field = config.get("password_field", "password")

        try:
            _, _, session_record = await cls._signin_session(
                **{identifier_field: identifier_value, password_field: old_password}
            )
        except SurrealDbError:
            raise SurrealDbError("Invalid current password") from None

        # Update only the record the signin authenticated, bound as the
        # session returned it: a RecordId rebuilt from the model stringifies
        # the key, so an integer key (``t:42``) would target ``t:⟨42⟩``.
        client = await SurrealDBConnectionManager.get_client(cls.get_connection_name())

        # Get encryption algorithm
        algorithm = config.get("encryption_algorithm", "argon2")

        result = await client.query(
            f"UPDATE type::record($record_id) SET {password_field} = crypto::{algorithm}::generate($new_password) RETURN id",
            {"record_id": session_record, "new_password": new_password},
        )
        if len(result.all_records) != 1:
            raise SurrealDbError(f"Password change did not update the signed-in record {session_record}")

        return True

    @classmethod
    def get_access_name(cls) -> str:
        """
        Get the access definition name for this model.

        Returns the ``access_name`` from model config if set,
        otherwise falls back to ``{table_name}_auth``.

        Returns:
            Access name (e.g., ``"account"`` or ``"users_auth"``)
        """
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        return config.get("access_name") or f"{table_name.lower()}_auth"

    @classmethod
    async def grant_bearer_key(
        cls,
        *,
        user_id: str | None = None,
        access_name: str | None = None,
    ) -> dict[str, Any]:
        """
        Issue a bearer key via SurrealDB 3.0's ``ACCESS ... GRANT`` command.

        Bearer access must be defined with ``DEFINE ACCESS ... TYPE BEARER``
        before calling this method.

        Args:
            user_id: Optional record ID to associate the key with
                     (e.g., ``"service_accounts:worker1"``).
            access_name: Override the access name.  Defaults to the
                         model's configured ``access_name``.

        Returns:
            Dict with ``"id"`` (key ID), ``"key"`` (bearer token), and
            other metadata from SurrealDB.

        Example::

            key_info = await ServiceAccount.grant_bearer_key(
                user_id="service_accounts:worker1",
            )
            # {"id": "access_grant:...", "key": "surreal-bearer-...", ...}
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..utils import validate_identifier

        name = access_name or cls.get_access_name()
        validate_identifier(name, "access name")
        conn_name = cls.get_connection_name()
        client = await SurrealDBConnectionManager.get_client(conn_name)

        if user_id:
            validate_identifier(user_id.split(":")[-1], "user ID")
            sql = f"ACCESS {name} GRANT FOR USER {user_id}"
        else:
            sql = f"ACCESS {name} GRANT"

        result = await client.query(sql)
        records = result.all_records
        if records:
            return records[0]
        return {}

    @classmethod
    async def revoke_bearer_key(
        cls,
        key_id: str,
        *,
        access_name: str | None = None,
    ) -> None:
        """
        Revoke a previously issued bearer key.

        Args:
            key_id: The key ID to revoke (e.g., ``"key:xxx"``).
            access_name: Override the access name.

        Example::

            await ServiceAccount.revoke_bearer_key("key:abc123")
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..utils import validate_identifier

        name = access_name or cls.get_access_name()
        validate_identifier(name, "access name")
        # key_id format: "access_grant:xxxx" — validate the ID part
        if ":" in key_id:
            validate_identifier(key_id.split(":")[0], "key ID table")
        else:
            validate_identifier(key_id, "key ID")
        conn_name = cls.get_connection_name()
        client = await SurrealDBConnectionManager.get_client(conn_name)

        sql = f"ACCESS {name} REVOKE {key_id}"
        await client.query(sql)
