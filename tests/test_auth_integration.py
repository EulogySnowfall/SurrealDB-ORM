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
    clear_model_registry,
)
from src.surreal_orm.types import TableType
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.auth import AuthenticatedUserMixin, AccessDefinition, AccessGenerator


SURREALDB_URL = "http://localhost:8001"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "test"
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
