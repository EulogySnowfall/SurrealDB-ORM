"""
Authentication mixins for SurrealDB ORM models.

Provides signup/signin methods for USER type models using
SurrealDB's native JWT authentication.
"""

from typing import TYPE_CHECKING, Any, Self

from surreal_sdk.exceptions import AuthenticationError, QueryError, SurrealDBError

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

        # Validate a token and get the record ID
        record_id = await User.validate_token(token)
        # "users:johndoe"
    """

    # Stub for mypy — overridden by BaseSurrealModel.get_connection_name()
    @classmethod
    def get_connection_name(cls) -> str:  # pragma: no cover
        return "default"

    @classmethod
    async def _create_auth_client(cls) -> "HTTPConnection":
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

        conn_name = cls.get_connection_name()  # type: ignore[attr-defined]
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
    async def signup(
        cls,
        **credentials: Any,
    ) -> tuple[Self, str]:
        """
        Create a new user via DEFINE ACCESS signup.

        This method uses SurrealDB's native signup functionality, which:
        1. Validates the credentials against the ACCESS definition
        2. Creates the user record with encrypted password
        3. Returns a JWT token

        An ephemeral connection is used so the root singleton is not affected.

        Args:
            **credentials: User credentials matching the ACCESS definition
                          (e.g., email, password, name, language)

        Returns:
            Tuple of (created user instance, JWT token)

        Raises:
            SurrealDbError: If signup fails (e.g., duplicate identifier)

        Example:
            user, token = await User.signup(
                email="user@example.com",
                password="secure_password",
                name="John Doe",
            )
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        # Get access configuration from model
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        access_name = config.get("access_name") or f"{table_name.lower()}_auth"

        # Get connection info from named connection config
        conn_name = cls.get_connection_name()  # type: ignore[attr-defined]
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
        except (SurrealDBError, AuthenticationError, QueryError) as e:
            raise SurrealDbError(f"Signup failed: {e}") from e
        finally:
            await client.close()

        # Fetch the created user via the root singleton (guaranteed access)
        root_client = await SurrealDBConnectionManager.get_client(conn_name)  # type: ignore[attr-defined]

        identifier_field = config.get("identifier_field", "email")
        identifier_value = credentials.get(identifier_field)

        if not identifier_value:
            raise SurrealDbError(f"Missing required field: {identifier_field}")

        result = await root_client.query(
            f"SELECT * FROM {table_name} WHERE {identifier_field} = $identifier",
            {"identifier": identifier_value},
        )

        if result.is_empty:  # type: ignore[attr-defined]
            raise cls.DoesNotExist("User not found after signup")  # type: ignore[attr-defined]

        user = cls.from_db(result.first)  # type: ignore
        return user, token

    @classmethod
    async def signin(
        cls,
        **credentials: Any,
    ) -> tuple[Self, str]:
        """
        Authenticate a user via DEFINE ACCESS signin.

        This method uses SurrealDB's native signin functionality, which:
        1. Validates credentials against the ACCESS definition
        2. Compares the password using the configured algorithm
        3. Returns a JWT token for subsequent authenticated requests

        An ephemeral connection is used so the root singleton is not affected.

        Args:
            **credentials: User credentials (identifier and password)

        Returns:
            Tuple of (user instance, JWT token)

        Raises:
            SurrealDbError: If signin fails (invalid credentials)

        Example:
            user, token = await User.signin(
                email="user@example.com",
                password="secure_password",
            )
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        # Get access configuration from model
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        access_name = config.get("access_name") or f"{table_name.lower()}_auth"

        # Get connection info from named connection config
        conn_name = cls.get_connection_name()  # type: ignore[attr-defined]
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
        except (SurrealDBError, AuthenticationError, QueryError) as e:
            raise SurrealDbError(f"Signin failed: {e}") from e
        finally:
            await client.close()

        # Fetch user via root singleton (guaranteed access)
        root_client = await SurrealDBConnectionManager.get_client(conn_name)  # type: ignore[attr-defined]

        identifier_field = config.get("identifier_field", "email")
        identifier_value = credentials.get(identifier_field)

        if not identifier_value:
            raise SurrealDbError(f"Missing required field: {identifier_field}")

        result = await root_client.query(
            f"SELECT * FROM {table_name} WHERE {identifier_field} = $identifier",
            {"identifier": identifier_value},
        )

        if result.is_empty:  # type: ignore[attr-defined]
            raise cls.DoesNotExist("User not found after signin")  # type: ignore[attr-defined]

        user = cls.from_db(result.first)  # type: ignore
        return user, token

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
        from ..connection_manager import SurrealDBConnectionManager

        client = await cls._create_auth_client()
        try:
            # Authenticate with the token on the ephemeral connection
            response = await client.authenticate(token)

            if not response.success:
                return None

            # Get the record ID from $auth (returns a RecordId scalar, not a list)
            auth_result = await client.query("RETURN $auth")
            first_qr = auth_result.first_result  # type: ignore[union-attr]
            if first_qr is None or first_qr.result is None:
                return None

            raw_auth = first_qr.result  # RecordId object from CBOR
            record_id = str(raw_auth)
        except (SurrealDBError, AuthenticationError, QueryError):
            return None
        finally:
            await client.close()

        # Fetch the full user record via root singleton
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__

        root_client = await SurrealDBConnectionManager.get_client(cls.get_connection_name())  # type: ignore[attr-defined]
        result = await root_client.query(
            f"SELECT * FROM {table_name} WHERE id = type::thing($record_id)",
            {"record_id": record_id},
        )

        if result.is_empty:  # type: ignore[attr-defined]
            return None

        user = cls.from_db(result.first)  # type: ignore
        return user, record_id

    @classmethod
    async def validate_token(
        cls,
        token: str,
    ) -> str | None:
        """
        Validate a JWT token and return the record ID.

        This is a lightweight alternative to :meth:`authenticate_token` that
        only validates the token and returns the ``$auth`` record ID without
        fetching the full user record.

        An ephemeral connection is used so the root singleton is not affected.

        Args:
            token: JWT token from previous signup/signin

        Returns:
            Record ID string (e.g., ``"users:johndoe"``) if valid, None otherwise

        Example:
            record_id = await User.validate_token(token)
            if record_id:
                print(f"Token belongs to {record_id}")
        """
        client = await cls._create_auth_client()
        try:
            response = await client.authenticate(token)

            if not response.success:
                return None

            auth_result = await client.query("RETURN $auth")
            first_qr = auth_result.first_result  # type: ignore[union-attr]
            if first_qr is None or first_qr.result is None:
                return None

            return str(first_qr.result)
        except (SurrealDBError, AuthenticationError, QueryError):
            return None
        finally:
            await client.close()

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
        connection), then updates the password via the root singleton.

        Args:
            identifier_value: Value of the identifier field (e.g., email)
            old_password: Current password for verification
            new_password: New password to set

        Returns:
            True if password was changed successfully

        Raises:
            SurrealDbError: If old password is incorrect
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        # First verify the old password by attempting signin
        config = getattr(cls, "model_config", {})
        identifier_field = config.get("identifier_field", "email")
        password_field = config.get("password_field", "password")

        try:
            await cls.signin(**{identifier_field: identifier_value, password_field: old_password})  # type: ignore[attr-defined]
        except SurrealDbError:
            raise SurrealDbError("Invalid current password") from None

        # Update the password via root singleton (needs root permissions)
        client = await SurrealDBConnectionManager.get_client(cls.get_connection_name())  # type: ignore[attr-defined]
        table_name = config.get("table_name") or cls.__name__

        # Get encryption algorithm
        algorithm = config.get("encryption_algorithm", "argon2")

        await client.query(
            f"""
            UPDATE {table_name}
            SET {password_field} = crypto::{algorithm}::generate($new_password)
            WHERE {identifier_field} = $identifier
            """,
            {"identifier": identifier_value, "new_password": new_password},
        )

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
