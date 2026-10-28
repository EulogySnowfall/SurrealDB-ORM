"""
Unit tests for authentication modules.

Tests AccessDefinition, AccessGenerator, and AuthenticatedUserMixin
using mocks instead of real database connections.
"""

import inspect
import sys
import time
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.auth.access import AccessDefinition, AccessGenerator
from src.surreal_orm.auth.mixins import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
    clear_model_registry,
)
from src.surreal_orm.types import EncryptionAlgorithm, TableType
from src.surreal_sdk.connection.base import BaseSurrealConnection
from src.surreal_sdk.types import AuthResponse


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
                signup_fields=["name"],
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

    def test_adds_created_at_when_field_exists(self) -> None:
        """Test that created_at is added to signup when the model has the field."""
        from datetime import datetime

        class SimpleUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted
            created_at: datetime | None = None

        access = AccessGenerator.from_model(SimpleUser)

        assert access is not None
        assert "created_at" in access.signup_fields
        assert access.signup_fields["created_at"] == "time::now()"

    def test_no_created_at_when_field_missing(self) -> None:
        """Test that created_at is NOT added when the model lacks the field."""

        class SimpleUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        access = AccessGenerator.from_model(SimpleUser)

        assert access is not None
        assert "created_at" not in access.signup_fields

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

    def test_sql_with_refresh(self) -> None:
        """Test SQL generation with WITH REFRESH enabled."""
        access = AccessDefinition(
            name="account",
            table="users",
            signup_fields={"email": "$email", "password": "crypto::argon2::generate($password)"},
            with_refresh=True,
            duration_grant="30d",
        )
        sql = access.to_surreal_ql()

        assert "WITH REFRESH" in sql
        assert "FOR GRANT 30d" in sql
        # WITH REFRESH must appear before DURATION
        assert sql.index("WITH REFRESH") < sql.index("DURATION")

    def test_sql_without_refresh(self) -> None:
        """Test SQL generation without WITH REFRESH (default)."""
        access = AccessDefinition(
            name="account",
            table="users",
            signup_fields={"email": "$email"},
        )
        sql = access.to_surreal_ql()

        assert "WITH REFRESH" not in sql
        assert "FOR GRANT" not in sql

    def test_sql_with_refresh_custom_grant_duration(self) -> None:
        """Test SQL generation with custom grant duration."""
        access = AccessDefinition(
            name="account",
            table="users",
            signup_fields={"email": "$email"},
            with_refresh=True,
            duration_grant="7d",
            duration_token="1h",
            duration_session="24h",
        )
        sql = access.to_surreal_ql()

        assert "FOR TOKEN 1h" in sql
        assert "FOR SESSION 24h" in sql
        assert "FOR GRANT 7d" in sql


class TestAccessGeneratorWithRefresh:
    """Tests for AccessGenerator with_refresh config support."""

    def test_generator_reads_with_refresh_from_config(self) -> None:
        """AccessGenerator reads with_refresh from model config."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                access_name="account",
                with_refresh=True,
                grant_duration="14d",
            )
            id: str | None = None
            email: str
            password: Encrypted

        access = AccessGenerator.from_model(TestUser)
        assert access is not None
        assert access.with_refresh is True
        assert access.duration_grant == "14d"

        sql = access.to_surreal_ql()
        assert "WITH REFRESH" in sql
        assert "FOR GRANT 14d" in sql

    def test_generator_defaults_no_refresh(self) -> None:
        """AccessGenerator defaults to with_refresh=False."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        access = AccessGenerator.from_model(TestUser)
        assert access is not None
        assert access.with_refresh is False

        sql = access.to_surreal_ql()
        assert "WITH REFRESH" not in sql


class TestDefineAccessMethod:
    """Tests for AuthenticatedUserMixin.define_access() classmethod."""

    def test_define_access_exists_on_user_mixin(self) -> None:
        """define_access() is an async classmethod on AuthenticatedUserMixin."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        assert hasattr(TestUser, "define_access")
        import inspect

        assert inspect.iscoroutinefunction(TestUser.define_access)

    async def test_define_access_not_available_on_non_user(self) -> None:
        """define_access() raises TypeError on non-USER models."""

        class RegularModel(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.NORMAL)
            id: str | None = None
            name: str
            password: Encrypted

        with pytest.raises(TypeError, match="not a USER type table"):
            await RegularModel.define_access()


# =============================================================================
# v0.8.0 — Bug Fix Tests
# =============================================================================


class TestBug1EphemeralConnection:
    """Bug 1: signup/signin must use ephemeral connections, not the root singleton."""

    def test_create_auth_client_is_a_classmethod_coroutine(self) -> None:
        """_create_auth_client must be an async classmethod."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        assert hasattr(TestUser, "_create_auth_client")
        # classmethod wrapping a coroutine — unwrap to check
        fn = TestUser._create_auth_client.__func__  # type: ignore[attr-defined]
        assert inspect.iscoroutinefunction(fn)

    @pytest.mark.asyncio
    async def test_signup_uses_ephemeral_not_singleton(self) -> None:
        """signup() must call _create_auth_client, not SurrealDBConnectionManager.get_client for the signup RPC."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, signup_fields=["name"])
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # Mock ephemeral client
        mock_ephemeral = AsyncMock()
        mock_ephemeral.signup = AsyncMock(return_value=MagicMock(success=True, token="jwt_token", raw={}))
        mock_ephemeral.close = AsyncMock()

        # Mock root client (for post-signup SELECT)
        mock_root = AsyncMock()
        mock_result = MagicMock(
            is_empty=False,
            first={"id": "TestUser:1", "email": "a@b.com", "name": "A", "password": "$argon2hashed"},
        )
        mock_root.query = AsyncMock(return_value=mock_result)

        with (
            patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch(
                "src.surreal_orm.connection_manager.SurrealDBConnectionManager",
                new=MagicMock(
                    get_namespace=MagicMock(return_value="test"),
                    get_database=MagicMock(return_value="test"),
                    get_client=AsyncMock(return_value=mock_root),
                ),
            ),
        ):
            user, token = await TestUser.signup(email="a@b.com", password="secret", name="A")

            # Ephemeral was used for signup RPC
            mock_ephemeral.signup.assert_called_once()
            # Ephemeral was closed
            mock_ephemeral.close.assert_called_once()
            # Token returned
            assert token == "jwt_token"

    @pytest.mark.asyncio
    async def test_signin_uses_ephemeral_not_singleton(self) -> None:
        """signin() must call _create_auth_client, not SurrealDBConnectionManager.get_client for the signin RPC."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_ephemeral.signin = AsyncMock(return_value=MagicMock(success=True, token="jwt_signin", raw={}))
        mock_ephemeral.close = AsyncMock()

        mock_root = AsyncMock()
        mock_result = MagicMock(
            is_empty=False,
            first={"id": "TestUser:1", "email": "a@b.com", "password": "$argon2hashed"},
        )
        mock_root.query = AsyncMock(return_value=mock_result)

        with (
            patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch(
                "src.surreal_orm.connection_manager.SurrealDBConnectionManager",
                new=MagicMock(
                    get_namespace=MagicMock(return_value="test"),
                    get_database=MagicMock(return_value="test"),
                    get_client=AsyncMock(return_value=mock_root),
                ),
            ),
        ):
            user, token = await TestUser.signin(email="a@b.com", password="secret")

            mock_ephemeral.signin.assert_called_once()
            mock_ephemeral.close.assert_called_once()
            assert token == "jwt_signin"

    @pytest.mark.asyncio
    async def test_ephemeral_closed_on_signup_failure(self) -> None:
        """Ephemeral connection must be closed even if signup fails."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_ephemeral.signup = AsyncMock(return_value=MagicMock(success=False, token=None, raw="duplicate key"))
        mock_ephemeral.close = AsyncMock()

        with (
            patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch(
                "src.surreal_orm.connection_manager.SurrealDBConnectionManager",
                new=MagicMock(
                    get_namespace=MagicMock(return_value="test"),
                    get_database=MagicMock(return_value="test"),
                ),
            ),
        ):
            with pytest.raises(SurrealDbError, match="Signup failed"):
                await TestUser.signup(email="a@b.com", password="secret")

            # Ephemeral must still be closed
            mock_ephemeral.close.assert_called_once()


class TestBug2ConfigurableAccessName:
    """Bug 2: access_name must be configurable via SurrealConfigDict."""

    def test_access_name_from_config(self) -> None:
        """get_access_name returns configured access_name."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                access_name="account",
            )
            id: str | None = None
            email: str
            password: Encrypted

        assert TestUser.get_access_name() == "account"

    def test_access_name_fallback(self) -> None:
        """get_access_name falls back to {table}_auth when not configured."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        assert TestUser.get_access_name() == "testuser_auth"

    def test_access_name_with_custom_table(self) -> None:
        """get_access_name falls back using custom table_name."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="app_users",
            )
            id: str | None = None
            email: str
            password: Encrypted

        assert TestUser.get_access_name() == "app_users_auth"

    def test_access_generator_uses_config_access_name(self) -> None:
        """AccessGenerator.from_model uses access_name from config."""

        class TestUser(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                access_name="my_custom_access",
            )
            id: str | None = None
            email: str
            password: Encrypted

        access = AccessGenerator.from_model(TestUser)
        assert access is not None
        assert access.name == "my_custom_access"

    def test_access_generator_fallback_without_config(self) -> None:
        """AccessGenerator.from_model falls back to {table}_auth."""

        class TestUser(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        access = AccessGenerator.from_model(TestUser)
        assert access is not None
        assert access.name == "testuser_auth"

    def test_access_definition_with_custom_name_generates_correct_sql(self) -> None:
        """AccessDefinition with custom name generates correct DEFINE ACCESS."""
        access = AccessDefinition(
            name="account",
            table="User",
            signup_fields={"email": "$email", "password": "crypto::argon2::generate($password)"},
        )
        sql = access.to_surreal_ql()
        assert "DEFINE ACCESS account ON DATABASE TYPE RECORD" in sql


class TestBug3SignupReturnsToken:
    """Bug 3: signup() must return AuthResult (backward-compatible with tuple unpacking)."""

    def test_signup_return_annotation(self) -> None:
        """signup() return annotation must be AuthResult[Self]."""
        hints = AuthenticatedUserMixin.signup.__annotations__
        assert "return" in hints
        assert "AuthResult" in str(hints["return"])

    @pytest.mark.asyncio
    async def test_signup_returns_user_and_token(self) -> None:
        """signup() must return AuthResult (unpackable as 2-tuple)."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, signup_fields=["name"])
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        mock_ephemeral = AsyncMock()
        mock_ephemeral.signup = AsyncMock(
            return_value=MagicMock(success=True, token="my_jwt_token", refresh_token="my_refresh", raw={})
        )
        mock_ephemeral.close = AsyncMock()

        mock_root = AsyncMock()
        mock_result = MagicMock(
            is_empty=False,
            first={"id": "TestUser:1", "email": "a@b.com", "name": "A", "password": "$argon2hashed"},
        )
        mock_root.query = AsyncMock(return_value=mock_result)

        with (
            patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch(
                "src.surreal_orm.connection_manager.SurrealDBConnectionManager",
                new=MagicMock(
                    get_namespace=MagicMock(return_value="test"),
                    get_database=MagicMock(return_value="test"),
                    get_client=AsyncMock(return_value=mock_root),
                ),
            ),
        ):
            result = await TestUser.signup(email="a@b.com", password="secret", name="A")
            # AuthResult is backward-compatible with tuple unpacking
            assert len(result) == 2
            user, token = result
            assert isinstance(user, TestUser)
            assert token == "my_jwt_token"
            # New: refresh_token accessible via attribute
            assert result.refresh_token == "my_refresh"


class TestBug4AuthenticateAndValidateToken:
    """Bug 4: authenticate() in SDK + authenticate_token/validate_token in mixin."""

    def test_sdk_has_authenticate_method(self) -> None:
        """BaseSurrealConnection must have an authenticate() method."""
        assert hasattr(BaseSurrealConnection, "authenticate")
        fn = BaseSurrealConnection.authenticate
        assert inspect.iscoroutinefunction(fn)

    def test_authenticate_token_return_annotation(self) -> None:
        """authenticate_token() must return tuple[Self, str] | None."""
        hints = AuthenticatedUserMixin.authenticate_token.__annotations__
        assert "return" in hints
        ret = str(hints["return"])
        assert "tuple" in ret.lower()
        assert "None" in ret

    def test_validate_token_exists_and_is_coroutine(self) -> None:
        """validate_token() must exist and be an async method."""
        assert hasattr(AuthenticatedUserMixin, "validate_token")
        fn = AuthenticatedUserMixin.validate_token.__func__  # type: ignore[attr-defined]
        assert inspect.iscoroutinefunction(fn)

    def test_validate_token_return_annotation(self) -> None:
        """validate_token() must return str | None."""
        hints = AuthenticatedUserMixin.validate_token.__annotations__
        assert "return" in hints
        ret = str(hints["return"])
        assert "str" in ret
        assert "None" in ret

    @pytest.mark.asyncio
    async def test_authenticate_token_uses_ephemeral(self) -> None:
        """authenticate_token() must use ephemeral connection."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_ephemeral.authenticate = AsyncMock(return_value=AuthResponse(token="tok", success=True, raw={}))
        mock_first_qr = MagicMock(result="TestUser:1")
        mock_auth_result = MagicMock(first_result=mock_first_qr)
        mock_ephemeral.query = AsyncMock(return_value=mock_auth_result)
        mock_ephemeral.close = AsyncMock()
        mock_ephemeral.namespace, mock_ephemeral.database = _I331_NS, _I331_DB
        token = _i331_jwt({"ID": "TestUser:1", "AC": "testuser_auth"})

        mock_root = AsyncMock()
        mock_user_result = MagicMock(
            is_empty=False,
            first={"id": "TestUser:1", "email": "a@b.com", "password": "$argon2hashed"},
        )
        mock_root.query = AsyncMock(return_value=mock_user_result)

        with (
            patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch(
                "src.surreal_orm.connection_manager.SurrealDBConnectionManager",
                new=MagicMock(get_client=AsyncMock(return_value=mock_root)),
            ),
        ):
            result = await TestUser.authenticate_token(token)
            assert result is not None
            user, record_id = result
            assert isinstance(user, TestUser)
            assert record_id == "TestUser:1"
            mock_ephemeral.authenticate.assert_called_once_with(token)
            mock_ephemeral.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_token_returns_record_id(self) -> None:
        """validate_token() must return just the record ID string."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_ephemeral.authenticate = AsyncMock(return_value=AuthResponse(token="tok", success=True, raw={}))
        mock_first_qr = MagicMock(result="TestUser:abc")
        mock_auth_result = MagicMock(first_result=mock_first_qr)
        mock_ephemeral.query = AsyncMock(return_value=mock_auth_result)
        mock_ephemeral.close = AsyncMock()
        mock_ephemeral.namespace, mock_ephemeral.database = _I331_NS, _I331_DB
        token = _i331_jwt({"ID": "TestUser:abc", "AC": "testuser_auth"})

        with patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)):
            record_id = await TestUser.validate_token(token)
            assert record_id == "TestUser:abc"
            mock_ephemeral.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_validate_token_returns_none_on_failure(self) -> None:
        """validate_token() must return None when authentication fails."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_response = MagicMock()
        mock_response.success = False
        mock_ephemeral.authenticate = AsyncMock(return_value=mock_response)
        mock_ephemeral.close = AsyncMock()

        with patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)):
            result = await TestUser.validate_token("bad_token")
            assert result is None
            mock_ephemeral.close.assert_called_once()


class TestIssue332SessionRecordLookup:
    """#332 — signin()/signup() fetch the record the session belongs to.

    The user is read by the ``$auth`` record ID of the ephemeral session,
    never by a second lookup on ``identifier_field``: the DEFINE ACCESS
    SIGNIN/SIGNUP clause decides which record the token is for, and a lookup
    by identifier can select a different record when the value is not unique.
    """

    @staticmethod
    def _model() -> type:
        class U332User(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="U332User",
                identifier_field="email",
            )
            id: str | None = None
            email: str
            password: Encrypted

        return U332User

    @staticmethod
    def _ephemeral(auth_value: object) -> AsyncMock:
        mock_ephemeral = AsyncMock()
        response = MagicMock(success=True, token="jwt_332", refresh_token="refresh_332", raw={})
        mock_ephemeral.signin = AsyncMock(return_value=response)
        mock_ephemeral.signup = AsyncMock(return_value=response)
        mock_ephemeral.query = AsyncMock(return_value=MagicMock(first_result=MagicMock(result=auth_value)))
        mock_ephemeral.close = AsyncMock()
        return mock_ephemeral

    @staticmethod
    def _manager(mock_root: AsyncMock) -> MagicMock:
        return MagicMock(get_client=AsyncMock(return_value=mock_root))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["signin", "signup"])
    async def test_user_fetched_by_session_record_id(self, method: str) -> None:
        from src.surreal_sdk.protocol.cbor import RecordId

        model = self._model()
        session_record = RecordId(table="U332User", id="second")
        mock_ephemeral = self._ephemeral(session_record)
        mock_root = AsyncMock()
        mock_root.query = AsyncMock(
            return_value=MagicMock(
                is_empty=False, first={"id": "U332User:second", "email": "dup@example.com", "password": "$argon2id$hash"}
            )
        )

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=self._manager(mock_root)),
        ):
            result = await getattr(model, method)(email="dup@example.com", password="pw-two")

        # The session record is read on the ephemeral connection, before it is closed.
        mock_ephemeral.query.assert_awaited_once_with("RETURN $auth")
        mock_ephemeral.close.assert_awaited_once()

        # The root lookup is by record ID, bound as the decoded record value.
        mock_root.query.assert_awaited_once()
        sql, variables = mock_root.query.await_args.args
        assert sql == "SELECT * FROM U332User WHERE id = type::record($record_id)"
        assert variables == {"record_id": session_record}
        for call in mock_root.query.await_args_list:
            assert "email" not in call.args[0]
            assert "identifier" not in call.args[0]

        assert result.user.id == "second"
        assert result.token == "jwt_332"
        assert result.refresh_token == "refresh_332"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["signin", "signup"])
    async def test_unreadable_session_record_raises(self, method: str) -> None:
        model = self._model()
        mock_ephemeral = self._ephemeral(None)
        mock_root = AsyncMock()

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=self._manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError, match="session record"):
                await getattr(model, method)(email="dup@example.com", password="pw-two")

        mock_ephemeral.close.assert_awaited_once()
        # No fallback lookup by identifier.
        mock_root.query.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["signin", "signup"])
    async def test_session_record_query_error_raises(self, method: str) -> None:
        # The mixin imports the SDK as ``surreal_sdk``; raise that module's class.
        from surreal_sdk.exceptions import QueryError

        model = self._model()
        mock_ephemeral = self._ephemeral(None)
        mock_ephemeral.query = AsyncMock(side_effect=QueryError(message="denied"))
        mock_root = AsyncMock()

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=self._manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError):
                await getattr(model, method)(email="dup@example.com", password="pw-two")

        mock_ephemeral.close.assert_awaited_once()
        mock_root.query.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["signin", "signup"])
    async def test_missing_record_raises_does_not_exist(self, method: str) -> None:
        from src.surreal_sdk.protocol.cbor import RecordId

        model = self._model()
        mock_ephemeral = self._ephemeral(RecordId(table="U332User", id="gone"))
        mock_root = AsyncMock()
        mock_root.query = AsyncMock(return_value=MagicMock(is_empty=True))

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=self._manager(mock_root)),
        ):
            with pytest.raises(model.DoesNotExist):  # type: ignore[attr-defined]
                await getattr(model, method)(email="dup@example.com", password="pw-two")

        mock_root.query.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_identifier_not_required_in_credentials(self) -> None:
        """The lookup no longer depends on identifier_field being among the credentials."""
        from src.surreal_sdk.protocol.cbor import RecordId

        model = self._model()
        mock_ephemeral = self._ephemeral(RecordId(table="U332User", id="u1"))
        mock_root = AsyncMock()
        mock_root.query = AsyncMock(
            return_value=MagicMock(is_empty=False, first={"id": "U332User:u1", "email": "x", "password": "$argon2id$hash"})
        )

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=self._manager(mock_root)),
        ):
            result = await model.signin(username="x", password="pw")

        assert result.user.id == "u1"

    @pytest.mark.asyncio
    async def test_change_password_binds_session_record(self) -> None:
        """The UPDATE targets $auth as decoded, never an ID rebuilt from the model."""
        from src.surreal_sdk.protocol.cbor import RecordId

        model = self._model()
        session_record = RecordId(table="U332User", id=42)
        mock_ephemeral = self._ephemeral(session_record)
        mock_root = AsyncMock()
        mock_root.query = AsyncMock(return_value=MagicMock(all_records=[{"id": session_record}]))

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=self._manager(mock_root)),
        ):
            assert await model.change_password("int@example.com", "old", "new") is True

        mock_ephemeral.close.assert_awaited_once()
        # One root query: the UPDATE. No user fetch, no identifier lookup.
        mock_root.query.assert_awaited_once()
        sql, variables = mock_root.query.await_args.args
        assert sql.startswith("UPDATE type::record($record_id) SET password = crypto::argon2::generate($new_password)")
        assert sql.endswith("RETURN id")
        assert "email" not in sql
        assert variables == {"record_id": session_record, "new_password": "new"}
        assert variables["record_id"].id == 42

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rows", [[], [{"id": "U332User:a"}, {"id": "U332User:b"}]])
    async def test_change_password_raises_unless_one_row_updated(self, rows: list[dict[str, str]]) -> None:
        from src.surreal_sdk.protocol.cbor import RecordId

        model = self._model()
        mock_ephemeral = self._ephemeral(RecordId(table="U332User", id="gone"))
        mock_root = AsyncMock()
        mock_root.query = AsyncMock(return_value=MagicMock(all_records=rows))

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=self._manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError, match="did not update"):
                await model.change_password("x@example.com", "old", "new")

    @pytest.mark.asyncio
    async def test_change_password_empty_session_is_invalid_password(self) -> None:
        model = self._model()
        mock_ephemeral = self._ephemeral(None)
        mock_root = AsyncMock()

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=self._manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError, match="Invalid current password"):
                await model.change_password("x@example.com", "old", "new")

        mock_root.query.assert_not_awaited()


_I331_NS = "i331_ns"
_I331_DB = "i331_db"


def _i331_jwt(claims: dict[str, object]) -> str:
    """Build an unsigned JWT-shaped token carrying ``claims``.

    Like a token SurrealDB issues, it names its namespace and database
    (``_I331_NS`` / ``_I331_DB``, those of ``_i331_client``) and its access:
    by default the default access of the ``ID`` claim's table (#393).
    """
    import base64
    import json

    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    full: dict[str, object] = {"NS": _I331_NS, "DB": _I331_DB}
    record = claims.get("ID")
    if isinstance(record, str) and ":" in record:
        full["AC"] = f"{record.split(':', 1)[0].lower()}_auth"
    full.update(claims)
    return f"{part({'alg': 'HS512'})}.{part(full)}.c2ln"


def _i331_client(auth: object, *, success: bool = True) -> AsyncMock:
    """Ephemeral connection mock whose ``RETURN $auth`` yields ``auth``."""
    client = AsyncMock()
    client.authenticate = AsyncMock(return_value=AuthResponse(token="tok", success=success, raw={}))
    client.query = AsyncMock(return_value=MagicMock(first_result=MagicMock(result=auth)))
    client.close = AsyncMock()
    client.namespace, client.database = _I331_NS, _I331_DB
    return client


class TestIssue331TokenCacheExpiryAndTable:
    """#331 — validate_token() honours the token's exp and the model's table.

    A cached entry lives until ``min(ttl, exp)``; a token whose ``exp`` cannot
    be read is never cached. A ``$auth`` record from another table yields
    ``None``, and cache entries are keyed per table so one model never reads
    another model's entry.
    """

    @pytest.fixture(autouse=True)
    def _reset_cache(self) -> Iterator[None]:
        AuthenticatedUserMixin._token_cache.clear()
        AuthenticatedUserMixin._token_cache_ttl = 300
        AuthenticatedUserMixin._token_cache_max_size = 1000
        yield
        AuthenticatedUserMixin._token_cache.clear()

    @staticmethod
    def _models() -> tuple[type, type]:
        class I331Member(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i331_member")
            id: str | None = None
            email: str
            password: Encrypted

        class I331Staff(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i331_staff")
            id: str | None = None
            email: str
            password: Encrypted

        return I331Member, I331Staff

    async def test_cached_entry_not_returned_after_token_exp(self) -> None:
        """An entry cached for a token is dropped once the token's exp passes."""
        member, _ = self._models()
        now_wall, now_mono = 1_900_000_000.0, 5_000.0
        token = _i331_jwt({"ID": "i331_member:a", "exp": int(now_wall) + 10})

        clock = MagicMock()
        clock.time = MagicMock(return_value=now_wall)
        clock.monotonic = MagicMock(return_value=now_mono)
        first = _i331_client("i331_member:a")
        with (
            patch("src.surreal_orm.auth.mixins.time", clock),
            patch.object(member, "_create_auth_client", new=AsyncMock(return_value=first)),
        ):
            assert await member.validate_token(token) == "i331_member:a"

        # 20 s later (both clocks): the token expired 10 s ago, TTL is 300 s.
        clock.time.return_value = now_wall + 20
        clock.monotonic.return_value = now_mono + 20
        second = _i331_client(None, success=False)
        with (
            patch("src.surreal_orm.auth.mixins.time", clock),
            patch.object(member, "_create_auth_client", new=AsyncMock(return_value=second)),
        ):
            assert await member.validate_token(token) is None
        second.authenticate.assert_awaited_once_with(token)

    async def test_entry_deadline_is_bounded_by_exp(self) -> None:
        """A token expiring before the TTL is cached only until its exp."""
        member, _ = self._models()
        exp = int(time.time()) + 30
        token = _i331_jwt({"ID": "i331_member:a", "exp": exp})
        with patch.object(member, "_create_auth_client", new=AsyncMock(return_value=_i331_client("i331_member:a"))):
            assert await member.validate_token(token) == "i331_member:a"

        ((key, entry),) = AuthenticatedUserMixin._token_cache.items()
        assert key == member._token_cache_key(token)
        assert key[1:] == ("i331_member", "i331_member_auth", token)
        deadline = entry[1]
        assert deadline - time.monotonic() <= 31

    async def test_other_table_returns_none_and_is_not_cached(self) -> None:
        """A token whose $auth is in another table is rejected, not cached."""
        _, staff = self._models()
        token = _i331_jwt({"ID": "i331_member:a", "exp": int(time.time()) + 600})
        client = _i331_client("i331_member:a")
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await staff.validate_token(token) is None
        assert AuthenticatedUserMixin._token_cache == {}
        client.close.assert_awaited_once()

    async def test_other_table_record_id_object_returns_none(self) -> None:
        """The table check reads the decoded RecordId, not a string prefix."""
        from src.surreal_sdk.protocol.cbor import RecordId

        _, staff = self._models()
        token = _i331_jwt({"ID": "x", "exp": int(time.time()) + 600})
        # A key that *starts with* the staff table name must not pass.
        auth = RecordId(table="i331_member", id="i331_staff")
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=_i331_client(auth))):
            assert await staff.validate_token(token) is None

    async def test_same_table_record_id_object_is_accepted(self) -> None:
        """A decoded RecordId of the model's own table is accepted."""
        from src.surreal_sdk.protocol.cbor import RecordId

        member, _ = self._models()
        token = _i331_jwt({"ID": "i331_member:a", "exp": int(time.time()) + 600})
        auth = RecordId(table="i331_member", id="a")
        with patch.object(member, "_create_auth_client", new=AsyncMock(return_value=_i331_client(auth))):
            assert await member.validate_token(token) == "i331_member:a"

    async def test_cache_entry_of_one_model_not_returned_to_another(self) -> None:
        """The shared cache is keyed per table."""
        member, staff = self._models()
        token = _i331_jwt({"ID": "i331_member:a", "exp": int(time.time()) + 600})
        with patch.object(member, "_create_auth_client", new=AsyncMock(return_value=_i331_client("i331_member:a"))):
            assert await member.validate_token(token) == "i331_member:a"

        client = _i331_client("i331_member:a")
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await staff.validate_token(token) is None
        client.authenticate.assert_awaited_once_with(token)

    @pytest.mark.parametrize(
        "claims",
        [{"ID": "i331_member:a"}, {"ID": "i331_member:a", "exp": "soon"}, {"ID": "i331_member:a", "exp": True}],
        ids=["no-exp", "string-exp", "bool-exp"],
    )
    async def test_unreadable_exp_is_validated_but_not_cached(self, claims: dict[str, object]) -> None:
        """Without a readable exp the token is validated by the server every time.

        An opaque token is no longer accepted at all (its access cannot be
        read, #393): see ``tests/test_issue_393_unit.py``.
        """
        member, _ = self._models()
        token = _i331_jwt(claims)
        client = _i331_client("i331_member:a")
        with patch.object(member, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await member.validate_token(token) == "i331_member:a"
            assert await member.validate_token(token) == "i331_member:a"
        assert AuthenticatedUserMixin._token_cache == {}
        assert client.authenticate.await_count == 2

    async def test_authenticate_token_other_table_returns_none(self) -> None:
        """authenticate_token() rejects a $auth record of another table."""
        _, staff = self._models()
        fetch = AsyncMock(return_value=MagicMock())
        with (
            patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=_i331_client("i331_member:a"))),
            patch.object(staff, "_fetch_record_by_id", new=fetch),
        ):
            assert await staff.authenticate_token("tok") is None
        fetch.assert_not_awaited()

    async def test_invalidate_token_removes_every_table_entry(self) -> None:
        """invalidate_token_cache(token) drops the token for every model."""
        member, staff = self._models()
        token = _i331_jwt({"ID": "i331_member:a", "exp": int(time.time()) + 600})
        other = _i331_jwt({"ID": "i331_member:b", "exp": int(time.time()) + 600})
        with patch.object(member, "_create_auth_client", new=AsyncMock(return_value=_i331_client("i331_member:a"))):
            await member.validate_token(token)
        with patch.object(member, "_create_auth_client", new=AsyncMock(return_value=_i331_client("i331_member:b"))):
            await member.validate_token(other)
        # A token is accepted by one model only (#393): the staff entry for the
        # same token is planted, as a cache written before that check would be.
        AuthenticatedUserMixin._token_cache[staff._token_cache_key(token)] = (
            "i331_staff:a",
            time.monotonic() + 300,
            time.time() + 300,
        )
        assert len(AuthenticatedUserMixin._token_cache) == 3

        staff.invalidate_token_cache(token)

        assert list(AuthenticatedUserMixin._token_cache) == [member._token_cache_key(other)]


class TestIssue331TokenCachePerConnection:
    """#331 — cache entries are no longer shared between connections.

    The same model (or two models sharing a table name) can be routed to
    different databases via ``using()`` or ``connection=``.  A token validated
    on one connection must be validated again by the server of another.
    """

    @pytest.fixture(autouse=True)
    def _registry_and_cache(self) -> Iterator[None]:
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        mgr = SurrealDBConnectionManager
        saved_configs = dict(mgr._configs)
        for name, db in (("i331_a", "i331_db_a"), ("i331_b", "i331_db_b")):
            mgr.add_connection(
                name,
                url="http://localhost:8000",
                user="root",
                password="root",
                namespace="i331_ns",
                database=db,
            )
        AuthenticatedUserMixin._token_cache.clear()
        AuthenticatedUserMixin._token_cache_ttl = 300
        AuthenticatedUserMixin._token_cache_max_size = 1000
        yield
        AuthenticatedUserMixin._token_cache.clear()
        mgr._configs.clear()
        mgr._configs.update(saved_configs)

    @staticmethod
    def _model() -> type:
        class I331ConnUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i331_conn_user")
            id: str | None = None
            email: str
            password: Encrypted

        return I331ConnUser

    async def test_entry_cached_on_one_connection_not_served_on_another(self) -> None:
        """A token cached through connection A is re-validated through B."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        model = self._model()
        token = _i331_jwt({"ID": "i331_conn_user:a", "exp": int(time.time()) + 600})
        with patch.object(model, "_create_auth_client", new=AsyncMock(return_value=_i331_client("i331_conn_user:a"))):
            async with SurrealDBConnectionManager.using("i331_a"):
                assert await model.validate_token(token) == "i331_conn_user:a"

        refused = _i331_client(None, success=False)
        with patch.object(model, "_create_auth_client", new=AsyncMock(return_value=refused)):
            async with SurrealDBConnectionManager.using("i331_b"):
                assert await model.validate_token(token) is None
        refused.authenticate.assert_awaited_once_with(token)

    async def test_entry_still_served_on_the_same_connection(self) -> None:
        """Caching on one connection keeps working for that connection."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        model = self._model()
        token = _i331_jwt({"ID": "i331_conn_user:a", "exp": int(time.time()) + 600})
        client = _i331_client("i331_conn_user:a")
        with patch.object(model, "_create_auth_client", new=AsyncMock(return_value=client)):
            async with SurrealDBConnectionManager.using("i331_a"):
                assert await model.validate_token(token) == "i331_conn_user:a"
                assert await model.validate_token(token) == "i331_conn_user:a"
        client.authenticate.assert_awaited_once_with(token)

    async def test_invalidate_token_removes_entries_of_every_connection(self) -> None:
        """invalidate_token_cache(token) drops the token on every connection."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        model = self._model()
        token = _i331_jwt({"ID": "i331_conn_user:a", "exp": int(time.time()) + 600})
        other = _i331_jwt({"ID": "i331_conn_user:b", "exp": int(time.time()) + 600})
        for name in ("i331_a", "i331_b"):
            async with SurrealDBConnectionManager.using(name):
                with patch.object(model, "_create_auth_client", new=AsyncMock(return_value=_i331_client("i331_conn_user:a"))):
                    await model.validate_token(token)
        async with SurrealDBConnectionManager.using("i331_a"):
            with patch.object(model, "_create_auth_client", new=AsyncMock(return_value=_i331_client("i331_conn_user:b"))):
                await model.validate_token(other)
            other_key = model._token_cache_key(other)
        assert len(AuthenticatedUserMixin._token_cache) == 3

        model.invalidate_token_cache(token)

        assert list(AuthenticatedUserMixin._token_cache) == [other_key]

    def test_model_connection_name_is_not_shadowed_by_the_mixin(self) -> None:
        """Auth calls follow the model's connection (using(), then connection=)."""

        class I331RoutedUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i331_routed_user", connection="i331_b")
            id: str | None = None
            email: str
            password: Encrypted

        assert I331RoutedUser.get_connection_name() == "i331_b"
        assert I331RoutedUser._token_cache_key("t")[0] == ("i331_b", "http://localhost:8000", "i331_ns", "i331_db_b")
        assert self._model().get_connection_name() == "default"

    async def test_using_overrides_the_auth_connection(self) -> None:
        """using() reroutes the auth connection and the cache key."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        model = self._model()
        async with SurrealDBConnectionManager.using("i331_a"):
            assert model.get_connection_name() == "i331_a"
            assert model._token_cache_key("t")[0][3] == "i331_db_a"
