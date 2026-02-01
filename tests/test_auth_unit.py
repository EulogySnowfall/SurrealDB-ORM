"""
Unit tests for authentication modules.

Tests AccessDefinition, AccessGenerator, and AuthenticatedUserMixin
using mocks instead of real database connections.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys

from src.surreal_orm.auth.access import AccessDefinition, AccessGenerator
from src.surreal_orm.auth.mixins import AuthenticatedUserMixin
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
    clear_model_registry,
)
from src.surreal_orm.types import EncryptionAlgorithm, TableType
from src.surreal_orm.fields import Encrypted


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear model registry before each test."""
    clear_model_registry()


class TestAccessDefinition:
    """Tests for AccessDefinition dataclass."""

    def test_basic_creation(self) -> None:
        """Test creating a basic access definition."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email", "password": "crypto::argon2::generate($password)"},
        )

        assert access.name == "user_auth"
        assert access.table == "User"
        assert access.identifier_field == "email"  # Default
        assert access.password_field == "password"  # Default
        assert access.duration_token == "15m"  # Default
        assert access.duration_session == "12h"  # Default
        assert access.algorithm == EncryptionAlgorithm.ARGON2  # Default

    def test_custom_fields(self) -> None:
        """Test access definition with custom fields."""
        access = AccessDefinition(
            name="admin_auth",
            table="Admin",
            identifier_field="username",
            password_field="secret",
            duration_token="1h",
            duration_session="24h",
            algorithm=EncryptionAlgorithm.BCRYPT,
            signup_fields={
                "username": "$username",
                "secret": "crypto::bcrypt::generate($secret)",
            },
        )

        assert access.identifier_field == "username"
        assert access.password_field == "secret"
        assert access.duration_token == "1h"
        assert access.duration_session == "24h"
        assert access.algorithm == EncryptionAlgorithm.BCRYPT

    def test_signin_where_auto_generated(self) -> None:
        """Test that signin_where is auto-generated if not provided."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            identifier_field="email",
            password_field="password",
            signup_fields={"email": "$email"},
        )

        assert access.signin_where is not None
        assert "email = $email" in access.signin_where
        assert "crypto::argon2::compare(password, $password)" in access.signin_where

    def test_signin_where_custom(self) -> None:
        """Test custom signin_where is preserved."""
        custom_where = "email = $email AND is_active = true AND crypto::argon2::compare(password, $password)"
        access = AccessDefinition(
            name="user_auth",
            table="User",
            signin_where=custom_where,
            signup_fields={"email": "$email"},
        )

        assert access.signin_where == custom_where

    def test_to_surreal_ql(self) -> None:
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
            duration_token="30m",
            duration_session="8h",
        )

        sql = access.to_surreal_ql()

        assert "DEFINE ACCESS user_auth ON DATABASE TYPE RECORD" in sql
        assert "SIGNUP (CREATE User SET" in sql
        assert "email = $email" in sql
        assert "crypto::argon2::generate($password)" in sql
        assert "name = $name" in sql
        assert "SIGNIN (SELECT * FROM User WHERE" in sql
        assert "crypto::argon2::compare(password, $password)" in sql
        assert "DURATION FOR TOKEN 30m, FOR SESSION 8h" in sql

    def test_to_remove_ql(self) -> None:
        """Test generating REMOVE ACCESS statement."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
        )

        sql = access.to_remove_ql()
        assert sql == "REMOVE ACCESS user_auth ON DATABASE;"

    def test_different_algorithms(self) -> None:
        """Test access definition with different algorithms."""
        for algo in EncryptionAlgorithm:
            access = AccessDefinition(
                name="test_auth",
                table="Test",
                algorithm=algo,
                signup_fields={"password": f"crypto::{algo}::generate($password)"},
            )
            sql = access.to_surreal_ql()
            assert f"crypto::{algo}" in sql


class TestAccessGenerator:
    """Tests for AccessGenerator class."""

    def test_from_user_model(self) -> None:
        """Test generating access from USER type model."""

        class TestUser(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                identifier_field="email",
                password_field="password",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        access = AccessGenerator.from_model(TestUser)

        assert access is not None
        assert access.name == "testuser_auth"
        assert access.table == "TestUser"
        assert access.identifier_field == "email"
        assert access.password_field == "password"
        assert "email" in access.signup_fields
        assert "password" in access.signup_fields
        assert "name" in access.signup_fields
        assert "crypto::argon2::generate($password)" in access.signup_fields["password"]

    def test_from_non_user_model(self) -> None:
        """Test that non-USER models return None."""

        class RegularModel(BaseSurrealModel):
            id: str | None = None
            name: str

        access = AccessGenerator.from_model(RegularModel)
        assert access is None

    def test_from_stream_model(self) -> None:
        """Test that STREAM models return None."""

        class StreamModel(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.STREAM)
            id: str | None = None
            data: str

        access = AccessGenerator.from_model(StreamModel)
        assert access is None

    def test_custom_table_name(self) -> None:
        """Test access generation with custom table name."""

        class MyUser(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="app_users",
            )
            id: str | None = None
            email: str
            password: Encrypted

        access = AccessGenerator.from_model(MyUser)

        assert access is not None
        assert access.name == "app_users_auth"
        assert access.table == "app_users"

    def test_custom_identifier_field(self) -> None:
        """Test access generation with custom identifier field."""

        class AdminUser(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                identifier_field="username",
            )
            id: str | None = None
            username: str
            password: Encrypted

        access = AccessGenerator.from_model(AdminUser)

        assert access is not None
        assert access.identifier_field == "username"
        assert "username = $username" in access.signin_where

    def test_custom_durations(self) -> None:
        """Test access generation with custom durations."""

        class SecureUser(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                token_duration="5m",
                session_duration="1h",
            )
            id: str | None = None
            email: str
            password: Encrypted

        access = AccessGenerator.from_model(SecureUser)

        assert access is not None
        assert access.duration_token == "5m"
        assert access.duration_session == "1h"

    def test_adds_created_at(self) -> None:
        """Test that created_at is added to signup fields."""

        class SimpleUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        access = AccessGenerator.from_model(SimpleUser)

        assert access is not None
        assert "created_at" in access.signup_fields
        assert access.signup_fields["created_at"] == "time::now()"

    def test_generate_all(self) -> None:
        """Test generating access for all USER models."""

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

    def test_generate_all_empty(self) -> None:
        """Test generate_all with no USER models."""

        class RegularModel(BaseSurrealModel):
            id: str | None = None
            data: str

        definitions = AccessGenerator.generate_all([RegularModel])
        assert definitions == []


class TestAuthenticatedUserMixin:
    """Tests for AuthenticatedUserMixin methods."""

    def test_get_access_name(self) -> None:
        """Test get_access_name method."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        assert TestUser.get_access_name() == "testuser_auth"

    def test_get_access_name_custom_table(self) -> None:
        """Test get_access_name with custom table name."""

        class CustomUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="my_users",
            )
            id: str | None = None
            email: str
            password: Encrypted

        assert CustomUser.get_access_name() == "my_users_auth"


class TestAuthenticatedUserMixinAsync:
    """Async tests for AuthenticatedUserMixin - mocking connection manager."""

    @pytest.fixture
    def mock_connection_manager(self):
        """Create mock for SurrealDBConnectionManager."""
        mock_client = AsyncMock()

        # Create mock manager
        mock_manager = MagicMock()
        mock_manager.get_client = AsyncMock(return_value=mock_client)
        mock_manager.get_namespace = MagicMock(return_value="test_ns")
        mock_manager.get_database = MagicMock(return_value="test_db")

        return mock_manager, mock_client

    @pytest.mark.asyncio
    async def test_signup_success(self, mock_connection_manager) -> None:
        """Test successful signup."""
        mock_manager, mock_client = mock_connection_manager

        # Mock signup response
        mock_response = MagicMock()
        mock_response.success = True
        mock_response.raw = {}
        mock_client.signup = AsyncMock(return_value=mock_response)

        # Mock query response
        mock_result = MagicMock()
        mock_result.is_empty = False
        mock_result.first = {
            "id": "TestUser:123",
            "email": "test@example.com",
            "name": "Test User",
        }
        mock_client.query = AsyncMock(return_value=mock_result)

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        with patch.dict(
            sys.modules, {"src.surreal_orm.connection_manager": MagicMock(SurrealDBConnectionManager=mock_manager)}
        ):
            # Patch the import inside the method
            with patch.object(TestUser, "signup", new=AsyncMock()) as mock_signup:
                mock_signup.return_value = TestUser(
                    id="TestUser:123", email="test@example.com", name="Test User", password="hashed"
                )

                user = await TestUser.signup(
                    email="test@example.com",
                    password="secret123",
                    name="Test User",
                )

                assert user is not None
                assert user.email == "test@example.com"

    @pytest.mark.asyncio
    async def test_signin_success(self, mock_connection_manager) -> None:
        """Test successful signin."""
        mock_manager, mock_client = mock_connection_manager

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # Mock signin method
        with patch.object(TestUser, "signin", new=AsyncMock()) as mock_signin:
            mock_user = TestUser(id="TestUser:123", email="test@example.com", name="Test User", password="hashed")
            mock_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test.signature"
            mock_signin.return_value = (mock_user, mock_token)

            user, token = await TestUser.signin(
                email="test@example.com",
                password="secret123",
            )

            assert user is not None
            assert user.email == "test@example.com"
            assert token.startswith("eyJ")

    @pytest.mark.asyncio
    async def test_authenticate_token_success(self, mock_connection_manager) -> None:
        """Test successful token authentication."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        with patch.object(TestUser, "authenticate_token", new=AsyncMock()) as mock_auth:
            mock_auth.return_value = TestUser(id="TestUser:123", email="test@example.com", password="hashed")

            user = await TestUser.authenticate_token("valid_token")

            assert user is not None
            assert user.email == "test@example.com"

    @pytest.mark.asyncio
    async def test_authenticate_token_invalid(self, mock_connection_manager) -> None:
        """Test invalid token returns None."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        with patch.object(TestUser, "authenticate_token", new=AsyncMock()) as mock_auth:
            mock_auth.return_value = None

            user = await TestUser.authenticate_token("invalid_token")
            assert user is None

    @pytest.mark.asyncio
    async def test_change_password_success(self, mock_connection_manager) -> None:
        """Test successful password change."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        with patch.object(TestUser, "change_password", new=AsyncMock()) as mock_change:
            mock_change.return_value = True

            result = await TestUser.change_password(
                identifier_value="test@example.com",
                old_password="old_pass",
                new_password="new_pass",
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_change_password_wrong_old_password(self, mock_connection_manager) -> None:
        """Test password change fails with wrong old password."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        with patch.object(TestUser, "change_password", new=AsyncMock()) as mock_change:
            mock_change.side_effect = SurrealDbError("Invalid current password")

            with pytest.raises(SurrealDbError):
                await TestUser.change_password(
                    identifier_value="test@example.com",
                    old_password="wrong_old",
                    new_password="new_pass",
                )


class TestAccessDefinitionEquality:
    """Tests for AccessDefinition comparison."""

    def test_equal_definitions(self) -> None:
        """Test two identical definitions are equal."""
        access1 = AccessDefinition(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
        )
        access2 = AccessDefinition(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
        )

        assert access1 == access2

    def test_different_names(self) -> None:
        """Test definitions with different names are not equal."""
        access1 = AccessDefinition(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
        )
        access2 = AccessDefinition(
            name="admin_auth",
            table="User",
            signup_fields={"email": "$email"},
        )

        assert access1 != access2

    def test_different_durations(self) -> None:
        """Test definitions with different durations are not equal."""
        access1 = AccessDefinition(
            name="user_auth",
            table="User",
            duration_token="15m",
            signup_fields={"email": "$email"},
        )
        access2 = AccessDefinition(
            name="user_auth",
            table="User",
            duration_token="1h",
            signup_fields={"email": "$email"},
        )

        assert access1 != access2


class TestAccessDefinitionSQLGeneration:
    """Tests for SQL generation edge cases."""

    def test_sql_with_special_characters_in_table(self) -> None:
        """Test SQL generation with table name."""
        access = AccessDefinition(
            name="user_auth",
            table="MyTable",
            signup_fields={"email": "$email"},
        )
        sql = access.to_surreal_ql()
        assert "CREATE MyTable SET" in sql

    def test_sql_with_multiple_signup_fields(self) -> None:
        """Test SQL with many signup fields."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            signup_fields={
                "email": "$email",
                "password": "crypto::argon2::generate($password)",
                "name": "$name",
                "role": "'user'",
                "created_at": "time::now()",
            },
        )
        sql = access.to_surreal_ql()

        # All fields should be in the signup
        assert "email = $email" in sql
        assert "password = crypto::argon2::generate($password)" in sql
        assert "name = $name" in sql
        assert "role = 'user'" in sql
        assert "created_at = time::now()" in sql

    def test_sql_with_bcrypt(self) -> None:
        """Test SQL generation with bcrypt algorithm."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            algorithm=EncryptionAlgorithm.BCRYPT,
            signup_fields={"password": "crypto::bcrypt::generate($password)"},
        )
        sql = access.to_surreal_ql()

        assert "crypto::bcrypt::compare(password, $password)" in sql

    def test_sql_with_pbkdf2(self) -> None:
        """Test SQL generation with pbkdf2 algorithm."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            algorithm=EncryptionAlgorithm.PBKDF2,
            signup_fields={"password": "crypto::pbkdf2::generate($password)"},
        )
        sql = access.to_surreal_ql()

        assert "crypto::pbkdf2::compare(password, $password)" in sql

    def test_sql_with_scrypt(self) -> None:
        """Test SQL generation with scrypt algorithm."""
        access = AccessDefinition(
            name="user_auth",
            table="User",
            algorithm=EncryptionAlgorithm.SCRYPT,
            signup_fields={"password": "crypto::scrypt::generate($password)"},
        )
        sql = access.to_surreal_ql()

        assert "crypto::scrypt::compare(password, $password)" in sql
