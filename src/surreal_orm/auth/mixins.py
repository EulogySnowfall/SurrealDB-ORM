"""
Authentication mixins for SurrealDB ORM models.

Provides signup/signin methods for USER type models using
SurrealDB's native JWT authentication.
"""

from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    pass


class AuthenticatedUserMixin:
    """
    Mixin providing authentication methods for User models.

    Add this mixin to your USER type models to enable signup/signin
    functionality using SurrealDB's DEFINE ACCESS authentication.

    Example:
        class User(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                identifier_field="email",
                password_field="password",
            )

            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # Create a new user
        user = await User.signup(email="test@example.com", password="secret", name="Test")

        # Authenticate existing user
        user, token = await User.signin(email="test@example.com", password="secret")
    """

    @classmethod
    async def signup(
        cls,
        **credentials: Any,
    ) -> Self:
        """
        Create a new user via DEFINE ACCESS signup.

        This method uses SurrealDB's native signup functionality, which:
        1. Validates the credentials against the ACCESS definition
        2. Creates the user record with encrypted password
        3. Returns a JWT token (stored internally)

        Args:
            **credentials: User credentials matching the model fields
                          (e.g., email, password, name)

        Returns:
            Created user instance

        Raises:
            SurrealDbError: If signup fails (e.g., duplicate email)

        Example:
            user = await User.signup(
                email="user@example.com",
                password="secure_password",
                name="John Doe"
            )
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        client = await SurrealDBConnectionManager.get_client()

        # Get access configuration from model
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        access_name = f"{table_name.lower()}_auth"

        # Get connection info
        namespace = SurrealDBConnectionManager.get_namespace()
        database = SurrealDBConnectionManager.get_database()

        if not namespace or not database:
            raise SurrealDbError("Namespace and database must be set for authentication")

        # Perform signup via SDK
        response = await client.signup(
            namespace=namespace,
            database=database,
            access=access_name,
            **credentials,
        )

        if not response.success:
            raise SurrealDbError(f"Signup failed: {response.raw}")

        # Fetch the created user
        identifier_field = config.get("identifier_field", "email")
        identifier_value = credentials.get(identifier_field)

        if not identifier_value:
            raise SurrealDbError(f"Missing required field: {identifier_field}")

        result = await client.query(
            f"SELECT * FROM {table_name} WHERE {identifier_field} = $identifier",
            {"identifier": identifier_value},
        )

        if result.is_empty:  # type: ignore[attr-defined]
            raise cls.DoesNotExist("User not found after signup")  # type: ignore[attr-defined]

        return cls.from_db(result.first)  # type: ignore

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

        Args:
            **credentials: User credentials (identifier and password)

        Returns:
            Tuple of (user instance, JWT token)

        Raises:
            SurrealDbError: If signin fails (invalid credentials)

        Example:
            user, token = await User.signin(
                email="user@example.com",
                password="secure_password"
            )
            # Use token for authenticated requests
        """
        from ..connection_manager import SurrealDBConnectionManager
        from ..model_base import SurrealDbError

        client = await SurrealDBConnectionManager.get_client()

        # Get access configuration from model
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        access_name = f"{table_name.lower()}_auth"

        # Get connection info
        namespace = SurrealDBConnectionManager.get_namespace()
        database = SurrealDBConnectionManager.get_database()

        if not namespace or not database:
            raise SurrealDbError("Namespace and database must be set for authentication")

        # Perform signin via SDK
        response = await client.signin(
            namespace=namespace,
            database=database,
            access=access_name,
            **credentials,
        )

        if not response.success or not response.token:
            raise SurrealDbError(f"Signin failed: {response.raw}")

        # Fetch user info
        identifier_field = config.get("identifier_field", "email")
        identifier_value = credentials.get(identifier_field)

        if not identifier_value:
            raise SurrealDbError(f"Missing required field: {identifier_field}")

        result = await client.query(
            f"SELECT * FROM {table_name} WHERE {identifier_field} = $identifier",
            {"identifier": identifier_value},
        )

        if result.is_empty:  # type: ignore[attr-defined]
            raise cls.DoesNotExist("User not found after signin")  # type: ignore[attr-defined]

        user = cls.from_db(result.first)  # type: ignore
        return user, response.token

    @classmethod
    async def authenticate_token(
        cls,
        token: str,
    ) -> Self | None:
        """
        Authenticate using an existing JWT token.

        Use this method to validate and authenticate a user from
        a previously obtained JWT token.

        Args:
            token: JWT token from previous signin

        Returns:
            User instance if token is valid, None otherwise

        Example:
            user = await User.authenticate_token(stored_token)
            if user:
                print(f"Authenticated as {user.email}")
        """
        from ..connection_manager import SurrealDBConnectionManager

        client = await SurrealDBConnectionManager.get_client()

        try:
            # Authenticate with the token
            response = await client.authenticate(token)  # type: ignore[attr-defined]

            if not response.success:
                return None

            # Get current user info using $auth variable
            config = getattr(cls, "model_config", {})
            table_name = config.get("table_name") or cls.__name__

            result = await client.query(f"SELECT * FROM {table_name} WHERE id = $auth.id")

            if result.is_empty:  # type: ignore[attr-defined]
                return None

            return cls.from_db(result.first)  # type: ignore

        except Exception:
            return None

    @classmethod
    async def change_password(
        cls,
        identifier_value: str,
        old_password: str,
        new_password: str,
    ) -> bool:
        """
        Change a user's password.

        Validates the old password before setting the new one.

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

        # Update the password (the access definition will handle encryption)
        client = await SurrealDBConnectionManager.get_client()
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

        Returns:
            Access name (e.g., "user_auth")
        """
        config = getattr(cls, "model_config", {})
        table_name = config.get("table_name") or cls.__name__
        return f"{table_name.lower()}_auth"
