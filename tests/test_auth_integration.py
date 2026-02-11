"""
Integration tests for authentication with SurrealDB.

These tests require a running SurrealDB instance.
Run with: pytest -m integration tests/test_auth_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
    clear_model_registry,
)
from src.surreal_orm.types import TableType
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.auth import AuthenticatedUserMixin, AccessDefinition, AccessGenerator
from tests.conftest import SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE

SURREALDB_DATABASE = "test_auth"


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    """Setup SurrealDB connection for tests."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


@pytest.fixture
def clean_registry() -> None:
    """Clear model registry for isolated tests."""
    clear_model_registry()


@pytest.fixture
async def clean_auth_database():
    """Clean up auth test tables before and after tests."""
    tables_and_access = [
        ("authuser_auth", "AuthUser"),
        ("testuser_auth", "TestUser"),
        ("appuser_auth", "AppUser"),
        ("account", "MixinUser"),
    ]

    async def cleanup() -> None:
        try:
            # Always reconnect as root before cleanup (tests may have changed auth context)
            client = await surreal_orm.SurrealDBConnectionManager.reconnect()
            if client is None:
                return
            for access_name, table_name in tables_and_access:
                try:
                    await client.query(f"REMOVE ACCESS IF EXISTS {access_name} ON DATABASE;")
                    await client.query(f"REMOVE TABLE IF EXISTS {table_name};")
                except Exception:
                    pass
        except Exception:
            pass

    # Setup: clean before tests
    await cleanup()

    yield  # Run test

    # Teardown: clean after tests and restore root auth
    await cleanup()


class TestAccessDefinition:
    """Tests for AccessDefinition class."""

    def test_access_definition_to_surreal_ql(self) -> None:
        """Test generating SurrealQL from access definition."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            signup_fields={
                "email": "$email",
                "password": "crypto::argon2::generate($password)",
                "name": "$name",
            },
            signin_where="email = $email AND crypto::argon2::compare(password, $password)",
            duration_token="1h",
            duration_session="24h",
        )

        sql = access.to_surreal_ql()

        assert "DEFINE ACCESS user_auth ON DATABASE TYPE RECORD" in sql
        assert "SIGNUP (CREATE User SET" in sql
        assert "email = $email" in sql
        assert "crypto::argon2::generate($password)" in sql
        assert "SIGNIN (SELECT * FROM User WHERE" in sql
        assert "crypto::argon2::compare(password, $password)" in sql
        assert "DURATION FOR TOKEN 1h, FOR SESSION 24h" in sql

    def test_access_definition_to_remove_ql(self) -> None:
        """Test generating remove statement."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
        )

        sql = access.to_remove_ql()
        assert sql == "REMOVE ACCESS user_auth ON DATABASE;"


class TestAccessGenerator:
    """Tests for AccessGenerator class."""

    def test_generate_from_user_model(self, clean_registry: None) -> None:
        """Test generating access from USER type model."""
        clear_model_registry()

        class TestUserModel(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                identifier_field="email",
                password_field="password",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        access = AccessGenerator.from_model(TestUserModel)

        assert access is not None
        assert access.name == "testusermodel_auth"
        assert access.table == "TestUserModel"
        assert access.signup_fields["email"] == "$email"
        assert "crypto::argon2::generate($password)" in access.signup_fields["password"]

    def test_returns_none_for_non_user_model(self, clean_registry: None) -> None:
        """Test that None is returned for non-USER type models."""
        clear_model_registry()

        class RegularModel(BaseSurrealModel):
            id: str | None = None
            name: str

        access = AccessGenerator.from_model(RegularModel)
        assert access is None

    def test_generate_all_user_models(self, clean_registry: None) -> None:
        """Test generating access for all USER models."""
        clear_model_registry()

        class UserA(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        class UserB(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            username: str
            password: Encrypted

        class RegularModel(BaseSurrealModel):
            id: str | None = None
            data: str

        definitions = AccessGenerator.generate_all([UserA, UserB, RegularModel])

        assert len(definitions) == 2
        names = [d.name for d in definitions]
        assert "usera_auth" in names
        assert "userb_auth" in names


@pytest.mark.integration
class TestAuthenticatedUserMixinIntegration:
    """Integration tests for AuthenticatedUserMixin."""

    async def test_create_auth_schema(self, clean_auth_database: None) -> None:
        """Test creating authentication schema in database."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        # Create the table and access definition manually for testing
        await client.query("""
            DEFINE TABLE AuthUser SCHEMAFULL;
            DEFINE FIELD email ON AuthUser TYPE string;
            DEFINE FIELD password ON AuthUser TYPE string;
            DEFINE FIELD name ON AuthUser TYPE string;
            DEFINE INDEX email_unique ON AuthUser FIELDS email UNIQUE;
        """)

        access = AccessDefinition(
            name="authuser_auth",
            table="AuthUser",
            signup_fields={
                "email": "$email",
                "password": "crypto::argon2::generate($password)",
                "name": "$name",
            },
            signin_where="email = $email AND crypto::argon2::compare(password, $password)",
        )

        await client.query(access.to_surreal_ql())

        # Verify access is defined
        result = await client.query("INFO FOR DATABASE;")
        assert result is not None

    async def test_signup_creates_user_with_hashed_password(self, clean_auth_database: None) -> None:
        """Test that signup creates user with properly hashed password."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        # Setup schema
        await client.query("""
            DEFINE TABLE TestUser SCHEMAFULL;
            DEFINE FIELD email ON TestUser TYPE string;
            DEFINE FIELD password ON TestUser TYPE string;
            DEFINE FIELD name ON TestUser TYPE string;
            DEFINE INDEX email_unique ON TestUser FIELDS email UNIQUE;

            DEFINE ACCESS testuser_auth ON DATABASE TYPE RECORD
                SIGNUP (CREATE TestUser SET
                    email = $email,
                    password = crypto::argon2::generate($password),
                    name = $name
                )
                SIGNIN (SELECT * FROM TestUser WHERE
                    email = $email AND
                    crypto::argon2::compare(password, $password)
                )
                DURATION FOR TOKEN 15m, FOR SESSION 12h;
        """)

        # Create user via direct signup (simulating what AuthenticatedUserMixin.signup does)
        response = await client.signup(
            namespace=SURREALDB_NAMESPACE,
            database=SURREALDB_DATABASE,
            access="testuser_auth",
            email="test@example.com",
            password="secret123",
            name="Test User",
        )

        assert response.success

        # Reconnect as root to verify user was created (signup changes auth context to new user)
        client = await surreal_orm.SurrealDBConnectionManager.reconnect()
        assert client is not None

        # Verify user was created with hashed password
        result = await client.query("SELECT * FROM TestUser WHERE email = 'test@example.com';")
        assert not result.is_empty

        user = result.first
        assert user["email"] == "test@example.com"
        assert user["name"] == "Test User"
        # Password should be hashed (starts with $argon2)
        assert user["password"].startswith("$argon2")

    async def test_signin_returns_token(self, clean_auth_database: None) -> None:
        """Test that signin returns JWT token."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        # Setup schema and create user
        await client.query("""
            DEFINE TABLE TestUser SCHEMAFULL;
            DEFINE FIELD email ON TestUser TYPE string;
            DEFINE FIELD password ON TestUser TYPE string;
            DEFINE FIELD name ON TestUser TYPE string;

            DEFINE ACCESS testuser_auth ON DATABASE TYPE RECORD
                SIGNUP (CREATE TestUser SET
                    email = $email,
                    password = crypto::argon2::generate($password),
                    name = $name
                )
                SIGNIN (SELECT * FROM TestUser WHERE
                    email = $email AND
                    crypto::argon2::compare(password, $password)
                )
                DURATION FOR TOKEN 15m, FOR SESSION 12h;
        """)

        # Signup first
        await client.signup(
            namespace=SURREALDB_NAMESPACE,
            database=SURREALDB_DATABASE,
            access="testuser_auth",
            email="signin_test@example.com",
            password="mypassword",
            name="Signin Test",
        )

        # Reconnect as root before signin (signup changes auth context)
        client = await surreal_orm.SurrealDBConnectionManager.reconnect()
        assert client is not None

        # Now signin as the user (for record access, password goes in credentials, not as 'pass')
        response = await client.signin(
            namespace=SURREALDB_NAMESPACE,
            database=SURREALDB_DATABASE,
            access="testuser_auth",
            email="signin_test@example.com",
            password="mypassword",  # This goes into credentials as 'password'
        )

        assert response.success
        assert response.token is not None
        # JWT tokens have 3 parts separated by dots
        assert len(response.token.split(".")) == 3

    async def test_signin_fails_with_wrong_password(self, clean_auth_database: None) -> None:
        """Test that signin fails with incorrect password."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        # Setup schema and create user
        await client.query("""
            DEFINE TABLE TestUser SCHEMAFULL;
            DEFINE FIELD email ON TestUser TYPE string;
            DEFINE FIELD password ON TestUser TYPE string;

            DEFINE ACCESS testuser_auth ON DATABASE TYPE RECORD
                SIGNUP (CREATE TestUser SET
                    email = $email,
                    password = crypto::argon2::generate($password)
                )
                SIGNIN (SELECT * FROM TestUser WHERE
                    email = $email AND
                    crypto::argon2::compare(password, $password)
                )
                DURATION FOR TOKEN 15m, FOR SESSION 12h;
        """)

        # Signup
        await client.signup(
            namespace=SURREALDB_NAMESPACE,
            database=SURREALDB_DATABASE,
            access="testuser_auth",
            email="wrong_pass@example.com",
            password="correct_password",
        )

        # Reconnect as root before signin (signup changes auth context)
        client = await surreal_orm.SurrealDBConnectionManager.reconnect()
        assert client is not None

        # Try signin with wrong password - should fail
        from surreal_sdk.exceptions import AuthenticationError

        try:
            response = await client.signin(
                namespace=SURREALDB_NAMESPACE,
                database=SURREALDB_DATABASE,
                access="testuser_auth",
                email="wrong_pass@example.com",
                password="wrong_password",
            )
            # If no exception, check that it failed
            assert response.success is False or response.token is None
        except AuthenticationError:
            # Expected - wrong password should cause auth failure
            pass


@pytest.mark.integration
class TestAuthWithModel:
    """Integration tests combining auth with ORM models."""

    async def test_full_auth_workflow(self, clean_auth_database: None) -> None:
        """Test complete authentication workflow with model."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        # Define model (simulating what would be in user's code)
        clear_model_registry()

        class AppUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="AppUser",
                identifier_field="email",
                password_field="password",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str
            is_active: bool = True

        # Setup schema manually (in real usage, this would be done via migrations)
        await client.query("""
            DEFINE TABLE AppUser SCHEMAFULL;
            DEFINE FIELD email ON AppUser TYPE string;
            DEFINE FIELD password ON AppUser TYPE string;
            DEFINE FIELD name ON AppUser TYPE string;
            DEFINE FIELD is_active ON AppUser TYPE bool DEFAULT true;
            DEFINE INDEX email_unique ON AppUser FIELDS email UNIQUE;

            DEFINE ACCESS appuser_auth ON DATABASE TYPE RECORD
                SIGNUP (CREATE AppUser SET
                    email = $email,
                    password = crypto::argon2::generate($password),
                    name = $name,
                    is_active = true
                )
                SIGNIN (SELECT * FROM AppUser WHERE
                    email = $email AND
                    crypto::argon2::compare(password, $password)
                )
                DURATION FOR TOKEN 1h, FOR SESSION 24h;
        """)

        # Test signup via SDK (simulating AuthenticatedUserMixin.signup)
        signup_response = await client.signup(
            namespace=SURREALDB_NAMESPACE,
            database=SURREALDB_DATABASE,
            access="appuser_auth",
            email="fulltest@example.com",
            password="secure123",
            name="Full Test User",
        )

        assert signup_response.success

        # Reconnect as root before signin (signup changes auth context)
        client = await surreal_orm.SurrealDBConnectionManager.reconnect()
        assert client is not None

        # Test signin via SDK (simulating AuthenticatedUserMixin.signin)
        signin_response = await client.signin(
            namespace=SURREALDB_NAMESPACE,
            database=SURREALDB_DATABASE,
            access="appuser_auth",
            email="fulltest@example.com",
            password="secure123",
        )

        assert signin_response.success
        assert signin_response.token is not None

        # Reconnect as root to query the table (signin changes auth context)
        client = await surreal_orm.SurrealDBConnectionManager.reconnect()
        assert client is not None

        # Fetch user data
        result = await client.query("SELECT * FROM AppUser WHERE email = 'fulltest@example.com';")

        assert not result.is_empty
        user_data = result.first

        # Create model instance from DB data
        user = AppUser.from_db(user_data)
        assert user.email == "fulltest@example.com"
        assert user.name == "Full Test User"
        assert user.is_active is True


@pytest.mark.integration
class TestMixinAuthWorkflow:
    """
    v0.8.0 integration tests — exercise the actual AuthenticatedUserMixin
    methods (signup, signin, authenticate_token, validate_token) against a
    real SurrealDB instance.

    These tests prove that:
    - Bug 1: Ephemeral connections don't corrupt the root singleton
    - Bug 2: Custom access_name is respected
    - Bug 3: signup() returns (user, token)
    - Bug 4: authenticate_token / validate_token work end-to-end
    """

    @pytest.fixture(autouse=True)
    async def _setup_mixin_schema(self, clean_auth_database: None) -> None:
        """Create schema + ACCESS definition for MixinUser before each test."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query("""
            DEFINE TABLE MixinUser SCHEMAFULL;
            DEFINE FIELD email ON MixinUser TYPE string;
            DEFINE FIELD password ON MixinUser TYPE string;
            DEFINE FIELD name ON MixinUser TYPE string;
            DEFINE INDEX email_unique ON MixinUser FIELDS email UNIQUE;

            DEFINE ACCESS account ON DATABASE TYPE RECORD
                SIGNUP (CREATE MixinUser SET
                    email = $email,
                    password = crypto::argon2::generate($password),
                    name = $name
                )
                SIGNIN (SELECT * FROM MixinUser WHERE
                    email = $email AND
                    crypto::argon2::compare(password, $password)
                )
                DURATION FOR TOKEN 1h, FOR SESSION 24h;
        """)

    def _make_model(self):
        """Create a fresh MixinUser model class (avoids registry conflicts)."""
        clear_model_registry()

        class MixinUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="MixinUser",
                identifier_field="email",
                password_field="password",
                access_name="account",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        return MixinUser

    async def test_signup_returns_user_and_token(self) -> None:
        """signup() must return (user_instance, jwt_token)."""
        MixinUser = self._make_model()

        user, token = await MixinUser.signup(
            email="signup_int@example.com",
            password="secret123",
            name="Signup Test",
        )

        # Bug 3: tuple returned
        assert isinstance(user, MixinUser)
        assert user.email == "signup_int@example.com"
        assert user.name == "Signup Test"
        assert isinstance(token, str)
        assert len(token.split(".")) == 3  # JWT format

    async def test_signup_does_not_corrupt_root_singleton(self) -> None:
        """After signup(), the root singleton must still work (Bug 1)."""
        MixinUser = self._make_model()

        await MixinUser.signup(
            email="nocorrupt@example.com",
            password="secret",
            name="No Corrupt",
        )

        # Root singleton should still be functional
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query("SELECT * FROM MixinUser WHERE email = 'nocorrupt@example.com';")
        assert not result.is_empty
        assert result.first["email"] == "nocorrupt@example.com"

    async def test_signin_returns_user_and_token(self) -> None:
        """signin() must return (user_instance, jwt_token)."""
        MixinUser = self._make_model()

        # First, create the user
        await MixinUser.signup(
            email="signin_int@example.com",
            password="mypassword",
            name="Signin Test",
        )

        # Now signin
        user, token = await MixinUser.signin(
            email="signin_int@example.com",
            password="mypassword",
        )

        assert isinstance(user, MixinUser)
        assert user.email == "signin_int@example.com"
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    async def test_signin_does_not_corrupt_root_singleton(self) -> None:
        """After signin(), the root singleton must still work (Bug 1)."""
        MixinUser = self._make_model()

        await MixinUser.signup(
            email="signin_nocorrupt@example.com",
            password="pass",
            name="NC",
        )

        await MixinUser.signin(
            email="signin_nocorrupt@example.com",
            password="pass",
        )

        # Root singleton should still be functional
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query("INFO FOR DATABASE;")
        assert result is not None

    async def test_signin_wrong_password_raises(self) -> None:
        """signin() must raise SurrealDbError on wrong password."""
        MixinUser = self._make_model()

        await MixinUser.signup(
            email="wrong_pass_int@example.com",
            password="correct",
            name="WP",
        )

        with pytest.raises(SurrealDbError):
            await MixinUser.signin(
                email="wrong_pass_int@example.com",
                password="wrong_password",
            )

    async def test_authenticate_token_returns_user_and_record_id(self) -> None:
        """authenticate_token() must return (user, record_id) for a valid token (Bug 4)."""
        MixinUser = self._make_model()

        _, token = await MixinUser.signup(
            email="authtoken@example.com",
            password="secret",
            name="Auth Token",
        )

        result = await MixinUser.authenticate_token(token)

        assert result is not None
        user, record_id = result
        assert isinstance(user, MixinUser)
        assert user.email == "authtoken@example.com"
        assert isinstance(record_id, str)
        assert "MixinUser:" in record_id

    async def test_authenticate_token_invalid_returns_none(self) -> None:
        """authenticate_token() must return None for an invalid token (Bug 4)."""
        MixinUser = self._make_model()

        result = await MixinUser.authenticate_token("invalid.jwt.token")
        assert result is None

    async def test_validate_token_returns_record_id(self) -> None:
        """validate_token() must return the record ID string for a valid token (Bug 4)."""
        MixinUser = self._make_model()

        _, token = await MixinUser.signup(
            email="validatetok@example.com",
            password="secret",
            name="Validate",
        )

        record_id = await MixinUser.validate_token(token)

        assert record_id is not None
        assert isinstance(record_id, str)
        assert "MixinUser:" in record_id

    async def test_validate_token_invalid_returns_none(self) -> None:
        """validate_token() must return None for an invalid token (Bug 4)."""
        MixinUser = self._make_model()

        result = await MixinUser.validate_token("bad.jwt.token")
        assert result is None

    async def test_custom_access_name_is_used(self) -> None:
        """Bug 2: The custom access_name='account' must be used, not 'mixinuser_auth'."""
        MixinUser = self._make_model()

        # This would fail if the mixin tried to use 'mixinuser_auth' instead of 'account'
        user, token = await MixinUser.signup(
            email="accessname@example.com",
            password="secret",
            name="Access Name Test",
        )

        assert user.email == "accessname@example.com"
        assert len(token.split(".")) == 3

    async def test_full_auth_lifecycle(self) -> None:
        """End-to-end: signup -> signin -> authenticate_token -> validate_token."""
        MixinUser = self._make_model()

        # 1. Signup
        user, signup_token = await MixinUser.signup(
            email="lifecycle@example.com",
            password="lifecycle_pass",
            name="Lifecycle",
        )
        assert user.email == "lifecycle@example.com"
        assert len(signup_token.split(".")) == 3

        # 2. Signin
        user2, signin_token = await MixinUser.signin(
            email="lifecycle@example.com",
            password="lifecycle_pass",
        )
        assert user2.email == "lifecycle@example.com"
        assert len(signin_token.split(".")) == 3

        # 3. Authenticate token (returns user + record_id)
        auth_result = await MixinUser.authenticate_token(signin_token)
        assert auth_result is not None
        auth_user, record_id = auth_result
        assert auth_user.email == "lifecycle@example.com"
        assert "MixinUser:" in record_id

        # 4. Validate token (lightweight — returns just record_id)
        validated_id = await MixinUser.validate_token(signin_token)
        assert validated_id is not None
        assert "MixinUser:" in validated_id
        assert validated_id == record_id

        # 5. Root singleton still works (Bug 1 — no corruption)
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query("SELECT count() FROM MixinUser GROUP ALL;")
        assert result is not None
