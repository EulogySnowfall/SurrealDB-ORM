"""
Integration tests for v0.30.0b1 Phase 2 features against SurrealDB 3.0.x.

Tests:
1. Auth with DURATION FOR GRANT (the actual SurrealDB 3.0 syntax)
2. Refresh token flow with WITH REFRESH clause
3. AuthResult backward-compatible 2-tuple unpacking
4. DEFINE API migration operations
5. REFERENCE clause on DEFINE FIELD (with ON DELETE strategies)

Run with: pytest -m integration tests/test_phase2_integration.py -v
"""

from collections.abc import AsyncGenerator
from typing import Any

import pytest

from src import surreal_orm
from src.surreal_orm.auth import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.migrations.operations import AddField, DefineApi, RemoveApi
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
)
from src.surreal_orm.types import TableType
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_phase2"


@pytest.fixture(scope="module", autouse=True)
async def setup_phase2_db() -> AsyncGenerator[None, Any]:
    """Initialize SurrealDB connection and ensure database exists."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )

    # Ensure namespace and database exist (SurrealDB 3.0 requirement)
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    try:
        await client.query(
            f"DEFINE NAMESPACE IF NOT EXISTS {SURREALDB_NAMESPACE}; DEFINE DATABASE IF NOT EXISTS {SURREALDB_DATABASE};"
        )
    except Exception:
        pass

    yield

    # Cleanup
    try:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(f"REMOVE DATABASE IF EXISTS {SURREALDB_DATABASE};")
    except Exception:
        pass


@pytest.fixture
async def clean_tables() -> AsyncGenerator[None, Any]:
    """Clean up test tables before and after each test."""
    tables = [
        "AuthUser",
        "DefAccessUser",
        "authors",
        "books",
        "persons",
        "licenses",
    ]
    access_names = ["authuser_auth", "auth_access", "defaccess_auth", "defaccess_refresh"]

    async def cleanup() -> None:
        try:
            client = await surreal_orm.SurrealDBConnectionManager.reconnect()
            if client is None:
                return
            for access_name in access_names:
                try:
                    await client.query(f"REMOVE ACCESS IF EXISTS {access_name} ON DATABASE;")
                except Exception:
                    pass
            for table in tables:
                try:
                    await client.query(f"REMOVE TABLE IF EXISTS {table};")
                except Exception:
                    pass
            # Remove API definitions
            for api in ['"/users/list"', '"/orders/create"']:
                try:
                    await client.query(f"REMOVE API IF EXISTS {api};")
                except Exception:
                    pass
        except Exception:
            pass

    await cleanup()
    yield
    await cleanup()


# =============================================================================
# 1. Auth with DURATION FOR GRANT + AuthResult backward compat
# =============================================================================


@pytest.mark.integration
class TestAuthWithGrantDuration:
    """
    Test auth signup/signin with DURATION FOR GRANT (SurrealDB 3.0 syntax).

    These tests use DURATION FOR GRANT without WITH REFRESH, so no
    refresh tokens are returned. See TestRefreshTokenFlow for the
    WITH REFRESH flow that returns refresh tokens.
    """

    async def test_signup_with_grant_duration(self, clean_tables: None) -> None:
        """DEFINE ACCESS with DURATION FOR GRANT should be accepted by SurrealDB 3.0."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        # Setup schema — DURATION FOR GRANT is the SurrealDB 3.0 syntax
        await client.query(
            "DEFINE TABLE AuthUser SCHEMAFULL;"
            " DEFINE FIELD email ON AuthUser TYPE string;"
            " DEFINE FIELD password ON AuthUser TYPE string;"
            " DEFINE FIELD name ON AuthUser TYPE string;"
            " DEFINE INDEX email_unique ON AuthUser FIELDS email UNIQUE;"
        )
        await client.query(
            "DEFINE ACCESS auth_access ON DATABASE TYPE RECORD"
            " SIGNUP (CREATE AuthUser SET"
            "   email = $email,"
            "   password = crypto::argon2::generate($password),"
            "   name = $name"
            " )"
            " SIGNIN (SELECT * FROM AuthUser WHERE"
            "   email = $email AND"
            "   crypto::argon2::compare(password, $password)"
            " )"
            " DURATION FOR TOKEN 15m, FOR SESSION 12h, FOR GRANT 30d;"
        )

        class AuthUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="AuthUser",
                identifier_field="email",
                password_field="password",
                access_name="auth_access",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        result = await AuthUser.signup(
            email="grant_signup@example.com",
            password="secure123",
            name="Grant Signup",
        )

        assert result.user.email == "grant_signup@example.com"
        assert result.token is not None
        assert len(result.token.split(".")) == 3
        # refresh_token is None on SurrealDB 3.0.2 (not yet implemented)
        # When SurrealDB adds refresh token support, this will become not None

    async def test_authresult_backward_compat_unpacking(self, clean_tables: None) -> None:
        """AuthResult supports 2-tuple unpacking (user, token) for backward compat."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE AuthUser SCHEMAFULL;"
            " DEFINE FIELD email ON AuthUser TYPE string;"
            " DEFINE FIELD password ON AuthUser TYPE string;"
            " DEFINE FIELD name ON AuthUser TYPE string;"
            " DEFINE INDEX email_unique ON AuthUser FIELDS email UNIQUE;"
        )
        await client.query(
            "DEFINE ACCESS auth_access ON DATABASE TYPE RECORD"
            " SIGNUP (CREATE AuthUser SET"
            "   email = $email,"
            "   password = crypto::argon2::generate($password),"
            "   name = $name"
            " )"
            " SIGNIN (SELECT * FROM AuthUser WHERE"
            "   email = $email AND"
            "   crypto::argon2::compare(password, $password)"
            " )"
            " DURATION FOR TOKEN 15m, FOR SESSION 12h;"
        )

        class AuthUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="AuthUser",
                identifier_field="email",
                password_field="password",
                access_name="auth_access",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # Old-style 2-tuple unpacking should still work
        user, token = await AuthUser.signup(
            email="compat_test@example.com",
            password="secure123",
            name="Compat Test",
        )

        assert isinstance(user, AuthUser)
        assert user.email == "compat_test@example.com"
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    async def test_signin_after_signup(self, clean_tables: None) -> None:
        """signin() returns AuthResult with valid JWT after signup."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE AuthUser SCHEMAFULL;"
            " DEFINE FIELD email ON AuthUser TYPE string;"
            " DEFINE FIELD password ON AuthUser TYPE string;"
            " DEFINE FIELD name ON AuthUser TYPE string;"
            " DEFINE INDEX email_unique ON AuthUser FIELDS email UNIQUE;"
        )
        await client.query(
            "DEFINE ACCESS auth_access ON DATABASE TYPE RECORD"
            " SIGNUP (CREATE AuthUser SET"
            "   email = $email,"
            "   password = crypto::argon2::generate($password),"
            "   name = $name"
            " )"
            " SIGNIN (SELECT * FROM AuthUser WHERE"
            "   email = $email AND"
            "   crypto::argon2::compare(password, $password)"
            " )"
            " DURATION FOR TOKEN 15m, FOR SESSION 12h;"
        )

        class AuthUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="AuthUser",
                identifier_field="email",
                password_field="password",
                access_name="auth_access",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        await AuthUser.signup(
            email="signin_test@example.com",
            password="secure123",
            name="Signin Test",
        )

        result = await AuthUser.signin(
            email="signin_test@example.com",
            password="secure123",
        )

        assert result.user.email == "signin_test@example.com"
        assert result.token is not None
        assert len(result.token.split(".")) == 3

    async def test_root_singleton_not_corrupted(self, clean_tables: None) -> None:
        """After signup/signin, root singleton should still work (ephemeral connections)."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE AuthUser SCHEMAFULL;"
            " DEFINE FIELD email ON AuthUser TYPE string;"
            " DEFINE FIELD password ON AuthUser TYPE string;"
            " DEFINE FIELD name ON AuthUser TYPE string;"
            " DEFINE INDEX email_unique ON AuthUser FIELDS email UNIQUE;"
        )
        await client.query(
            "DEFINE ACCESS auth_access ON DATABASE TYPE RECORD"
            " SIGNUP (CREATE AuthUser SET"
            "   email = $email,"
            "   password = crypto::argon2::generate($password),"
            "   name = $name"
            " )"
            " SIGNIN (SELECT * FROM AuthUser WHERE"
            "   email = $email AND"
            "   crypto::argon2::compare(password, $password)"
            " )"
            " DURATION FOR TOKEN 15m, FOR SESSION 12h;"
        )

        class AuthUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="AuthUser",
                identifier_field="email",
                password_field="password",
                access_name="auth_access",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        await AuthUser.signup(
            email="singleton@example.com",
            password="secure123",
            name="Singleton",
        )
        await AuthUser.signin(
            email="singleton@example.com",
            password="secure123",
        )

        # Root singleton should still be functional
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query("SELECT count() FROM AuthUser GROUP ALL;")
        assert result is not None


# =============================================================================
# 2. Refresh Token Flow (WITH REFRESH)
# =============================================================================


@pytest.mark.integration
class TestRefreshTokenFlow:
    """Integration tests for refresh token exchange (SurrealDB 3.0 WITH REFRESH)."""

    async def test_signup_returns_refresh_token(self, clean_tables: None) -> None:
        """signup() with WITH REFRESH should return both access and refresh tokens."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE AuthUser SCHEMAFULL;"
            " DEFINE FIELD email ON AuthUser TYPE string;"
            " DEFINE FIELD password ON AuthUser TYPE string;"
            " DEFINE FIELD name ON AuthUser TYPE string;"
            " DEFINE INDEX email_unique ON AuthUser FIELDS email UNIQUE;"
        )
        # WITH REFRESH must come after SIGNIN(...) and before DURATION
        await client.query(
            "DEFINE ACCESS auth_access ON DATABASE TYPE RECORD"
            " SIGNUP (CREATE AuthUser SET"
            "   email = $email,"
            "   password = crypto::argon2::generate($password),"
            "   name = $name"
            " )"
            " SIGNIN (SELECT * FROM AuthUser WHERE"
            "   email = $email AND"
            "   crypto::argon2::compare(password, $password)"
            " )"
            " WITH REFRESH"
            " DURATION FOR TOKEN 15m, FOR SESSION 12h, FOR GRANT 30d;"
        )

        class AuthUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="AuthUser",
                identifier_field="email",
                password_field="password",
                access_name="auth_access",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        result = await AuthUser.signup(
            email="refresh_signup@example.com",
            password="secure123",
            name="Refresh Signup",
        )

        assert result.token is not None
        assert len(result.token.split(".")) == 3
        # WITH REFRESH should produce a refresh token
        assert result.refresh_token is not None
        assert result.refresh_token.startswith("surreal-refresh-")

    async def test_signin_returns_refresh_token(self, clean_tables: None) -> None:
        """signin() with WITH REFRESH should return both access and refresh tokens."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE AuthUser SCHEMAFULL;"
            " DEFINE FIELD email ON AuthUser TYPE string;"
            " DEFINE FIELD password ON AuthUser TYPE string;"
            " DEFINE FIELD name ON AuthUser TYPE string;"
            " DEFINE INDEX email_unique ON AuthUser FIELDS email UNIQUE;"
        )
        await client.query(
            "DEFINE ACCESS auth_access ON DATABASE TYPE RECORD"
            " SIGNUP (CREATE AuthUser SET"
            "   email = $email,"
            "   password = crypto::argon2::generate($password),"
            "   name = $name"
            " )"
            " SIGNIN (SELECT * FROM AuthUser WHERE"
            "   email = $email AND"
            "   crypto::argon2::compare(password, $password)"
            " )"
            " WITH REFRESH"
            " DURATION FOR TOKEN 15m, FOR SESSION 12h, FOR GRANT 30d;"
        )

        class AuthUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="AuthUser",
                identifier_field="email",
                password_field="password",
                access_name="auth_access",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # First signup
        await AuthUser.signup(
            email="refresh_signin@example.com",
            password="secure123",
            name="Refresh Signin",
        )

        # Then signin
        result = await AuthUser.signin(
            email="refresh_signin@example.com",
            password="secure123",
        )

        assert result.token is not None
        assert len(result.token.split(".")) == 3
        assert result.refresh_token is not None
        assert result.refresh_token.startswith("surreal-refresh-")

    async def test_refresh_access_token(self, clean_tables: None) -> None:
        """refresh_access_token() should exchange a refresh token for new tokens."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE AuthUser SCHEMAFULL;"
            " DEFINE FIELD email ON AuthUser TYPE string;"
            " DEFINE FIELD password ON AuthUser TYPE string;"
            " DEFINE FIELD name ON AuthUser TYPE string;"
            " DEFINE INDEX email_unique ON AuthUser FIELDS email UNIQUE;"
        )
        await client.query(
            "DEFINE ACCESS auth_access ON DATABASE TYPE RECORD"
            " SIGNUP (CREATE AuthUser SET"
            "   email = $email,"
            "   password = crypto::argon2::generate($password),"
            "   name = $name"
            " )"
            " SIGNIN (SELECT * FROM AuthUser WHERE"
            "   email = $email AND"
            "   crypto::argon2::compare(password, $password)"
            " )"
            " WITH REFRESH"
            " DURATION FOR TOKEN 15m, FOR SESSION 12h, FOR GRANT 30d;"
        )

        class AuthUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="AuthUser",
                identifier_field="email",
                password_field="password",
                access_name="auth_access",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # Signup to get initial tokens
        signup_result = await AuthUser.signup(
            email="refresh_exchange@example.com",
            password="secure123",
            name="Refresh Exchange",
        )

        assert signup_result.refresh_token is not None

        # Exchange refresh token for new access + refresh tokens
        refresh_result = await AuthUser.refresh_access_token(signup_result.refresh_token)

        assert refresh_result.token is not None
        assert len(refresh_result.token.split(".")) == 3
        assert refresh_result.user.email == "refresh_exchange@example.com"
        # New refresh token should be different (rotation)
        assert refresh_result.refresh_token is not None
        assert refresh_result.refresh_token != signup_result.refresh_token


# =============================================================================
# 2a. define_table() ORM method
# =============================================================================


@pytest.mark.integration
class TestDefineTableMethod:
    """Integration tests for BaseSurrealModel.define_table() classmethod."""

    async def test_define_table_creates_schema(self, clean_tables: None) -> None:
        """define_table() should create the table and all fields in the database."""
        clear_model_registry()

        class DefAccessUser(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="DefAccessUser",
            )
            id: str | None = None
            email: str
            name: str
            age: int = 0

        sql = await DefAccessUser.define_table()

        assert "DEFINE TABLE DefAccessUser" in sql
        assert "DEFINE FIELD email ON DefAccessUser" in sql
        assert "DEFINE FIELD name ON DefAccessUser" in sql
        assert "DEFINE FIELD age ON DefAccessUser" in sql

        # Verify we can save a record
        user = DefAccessUser(email="test@example.com", name="Test", age=25)
        await user.save()

        loaded = await DefAccessUser.objects().first()
        assert loaded.email == "test@example.com"
        assert loaded.age == 25

    async def test_define_table_then_define_access_e2e(self, clean_tables: None) -> None:
        """define_table() + define_access() should enable full auth flow."""
        clear_model_registry()

        class DefAccessUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_name="DefAccessUser",
                table_type=TableType.USER,
                access_name="defaccess_auth",
                with_refresh=True,
                grant_duration="30d",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # ORM creates the table schema
        table_sql = await DefAccessUser.define_table()
        assert "SCHEMAFULL" in table_sql
        assert "crypto::argon2::generate" in table_sql

        # ORM creates the access definition
        access_sql = await DefAccessUser.define_access()
        assert "WITH REFRESH" in access_sql

        # Full auth flow works
        result = await DefAccessUser.signup(
            email="e2e@example.com",
            password="secure123",
            name="E2E Test",
        )
        assert result.user.email == "e2e@example.com"
        assert result.token is not None
        assert result.refresh_token is not None


# =============================================================================
# 2b. define_access() ORM method
# =============================================================================


@pytest.mark.integration
class TestDefineAccessMethod:
    """Integration tests for User.define_access() classmethod."""

    async def test_define_access_creates_access_definition(self, clean_tables: None) -> None:
        """define_access() should create the DEFINE ACCESS statement in the database."""
        clear_model_registry()

        # Create table first
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(
            "DEFINE TABLE DefAccessUser SCHEMAFULL;"
            " DEFINE FIELD email ON DefAccessUser TYPE string;"
            " DEFINE FIELD password ON DefAccessUser TYPE string;"
            " DEFINE FIELD name ON DefAccessUser TYPE string;"
            " DEFINE FIELD created_at ON DefAccessUser TYPE option<datetime>;"
            " DEFINE INDEX email_unique ON DefAccessUser FIELDS email UNIQUE;"
        )

        class DefAccessUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="DefAccessUser",
                access_name="defaccess_auth",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # Use ORM method instead of raw SQL
        sql = await DefAccessUser.define_access()

        assert "DEFINE ACCESS defaccess_auth" in sql
        assert "SIGNUP" in sql
        assert "SIGNIN" in sql

        # Verify signup works with the ORM-created access definition
        result = await DefAccessUser.signup(
            email="defaccess@example.com",
            password="secure123",
            name="Define Access Test",
        )
        assert result.user.email == "defaccess@example.com"
        assert result.token is not None

    async def test_define_access_with_refresh(self, clean_tables: None) -> None:
        """define_access() with with_refresh=True should produce WITH REFRESH."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(
            "DEFINE TABLE DefAccessUser SCHEMAFULL;"
            " DEFINE FIELD email ON DefAccessUser TYPE string;"
            " DEFINE FIELD password ON DefAccessUser TYPE string;"
            " DEFINE FIELD name ON DefAccessUser TYPE string;"
            " DEFINE FIELD created_at ON DefAccessUser TYPE option<datetime>;"
            " DEFINE INDEX email_unique ON DefAccessUser FIELDS email UNIQUE;"
        )

        class DefAccessUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="DefAccessUser",
                access_name="defaccess_refresh",
                with_refresh=True,
                grant_duration="30d",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        sql = await DefAccessUser.define_access()

        assert "WITH REFRESH" in sql
        assert "FOR GRANT 30d" in sql

        # Verify signup returns refresh token
        result = await DefAccessUser.signup(
            email="refresh_defaccess@example.com",
            password="secure123",
            name="Refresh Define Access",
        )
        assert result.token is not None
        assert result.refresh_token is not None
        assert result.refresh_token.startswith("surreal-refresh-")

    async def test_define_access_then_signin(self, clean_tables: None) -> None:
        """Full flow: define_access() → signup() → signin()."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(
            "DEFINE TABLE DefAccessUser SCHEMAFULL;"
            " DEFINE FIELD email ON DefAccessUser TYPE string;"
            " DEFINE FIELD password ON DefAccessUser TYPE string;"
            " DEFINE FIELD name ON DefAccessUser TYPE string;"
            " DEFINE FIELD created_at ON DefAccessUser TYPE option<datetime>;"
            " DEFINE INDEX email_unique ON DefAccessUser FIELDS email UNIQUE;"
        )

        class DefAccessUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="DefAccessUser",
                access_name="defaccess_auth",
                with_refresh=True,
                grant_duration="14d",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        await DefAccessUser.define_access()

        await DefAccessUser.signup(
            email="flow@example.com",
            password="secure123",
            name="Full Flow",
        )

        result = await DefAccessUser.signin(
            email="flow@example.com",
            password="secure123",
        )

        assert result.user.email == "flow@example.com"
        assert result.token is not None
        assert result.refresh_token is not None

    async def test_define_access_with_overrides(self, clean_tables: None) -> None:
        """define_access() keyword args should override model config values."""
        clear_model_registry()

        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(
            "DEFINE TABLE DefAccessUser SCHEMAFULL;"
            " DEFINE FIELD email ON DefAccessUser TYPE string;"
            " DEFINE FIELD password ON DefAccessUser TYPE string;"
            " DEFINE FIELD name ON DefAccessUser TYPE string;"
            " DEFINE FIELD created_at ON DefAccessUser TYPE option<datetime>;"
            " DEFINE INDEX email_unique ON DefAccessUser FIELDS email UNIQUE;"
        )

        class DefAccessUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="DefAccessUser",
                access_name="defaccess_auth",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        # Override durations and enable refresh at call site
        sql = await DefAccessUser.define_access(
            token_duration="1h",
            session_duration="24h",
            with_refresh=True,
            grant_duration="7d",
        )

        assert "defaccess_auth" in sql
        assert "FOR TOKEN 1h" in sql
        assert "FOR SESSION 24h" in sql
        assert "WITH REFRESH" in sql
        assert "FOR GRANT 7d" in sql

        # Signup works with the overridden access definition
        result = await DefAccessUser.signup(
            email="override@example.com",
            password="secure123",
            name="Override Test",
        )

        assert result.token is not None
        assert result.refresh_token is not None


# =============================================================================
# 3. DEFINE API Migration Operations
# =============================================================================


@pytest.mark.integration
class TestDefineApiIntegration:
    """Integration tests for DEFINE API against SurrealDB 3.0."""

    async def test_define_api_basic(self, clean_tables: None) -> None:
        """A basic DEFINE API statement should execute without error."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        op = DefineApi(
            name="/users/list",
            method="get",
            handler="SELECT * FROM users",
        )

        sql = op.forwards()
        assert 'DEFINE API "/users/list"' in sql

        # Execute against SurrealDB 3.0
        await client.query(sql)

        # Verify it exists in INFO FOR DB
        info = await client.query("INFO FOR DATABASE;")
        assert info is not None

    async def test_define_api_with_permissions(self, clean_tables: None) -> None:
        """DEFINE API with PERMISSIONS clause should execute."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        op = DefineApi(
            name="/orders/create",
            method="post",
            handler="CREATE orders SET data = $body",
            permissions="FULL",
        )

        sql = op.forwards()
        assert "PERMISSIONS FULL" in sql
        # PERMISSIONS must come before THEN in SurrealDB 3.0
        assert sql.index("PERMISSIONS") < sql.index("THEN")

        await client.query(sql)

        info = await client.query("INFO FOR DATABASE;")
        assert info is not None

    async def test_remove_api(self, clean_tables: None) -> None:
        """REMOVE API should remove a previously defined endpoint."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        # First define
        define_op = DefineApi(
            name="/users/list",
            method="get",
            handler="SELECT * FROM users",
        )
        await client.query(define_op.forwards())

        # Then remove
        remove_op = RemoveApi(name="/users/list")
        sql = remove_op.forwards()
        assert 'REMOVE API "/users/list"' in sql

        await client.query(sql)


# =============================================================================
# 4. REFERENCE Clause on DEFINE FIELD
# =============================================================================


@pytest.mark.integration
class TestReferenceClauseIntegration:
    """Integration tests for REFERENCE clause on DEFINE FIELD (SurrealDB 3.0)."""

    async def test_define_field_with_reference(self, clean_tables: None) -> None:
        """DEFINE FIELD ... REFERENCE should execute without error."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query("DEFINE TABLE books SCHEMALESS;")
        await client.query("DEFINE TABLE authors SCHEMALESS;")

        op = AddField(
            table="authors",
            name="books",
            field_type="option<array<record<books>>>",
            reference=True,
        )
        sql = op.forwards()
        assert "REFERENCE" in sql
        assert "ON DELETE" not in sql

        await client.query(sql)

        # Verify via INFO FOR TABLE
        info = await client.query("INFO FOR TABLE authors;")
        assert info is not None

    async def test_define_field_with_reference_on_delete_cascade(self, clean_tables: None) -> None:
        """DEFINE FIELD ... REFERENCE ON DELETE CASCADE should execute."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query("DEFINE TABLE persons SCHEMALESS;")
        await client.query("DEFINE TABLE licenses SCHEMALESS;")

        op = AddField(
            table="licenses",
            name="owner",
            field_type="option<record<persons>>",
            reference=True,
            on_delete="CASCADE",
        )
        sql = op.forwards()
        assert "REFERENCE" in sql
        assert "ON DELETE CASCADE" in sql

        await client.query(sql)

        info = await client.query("INFO FOR TABLE licenses;")
        assert info is not None

    async def test_cascade_deletes_referencing_record(self, clean_tables: None) -> None:
        """CASCADE should delete referencing records when the referenced record is deleted."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE persons SCHEMALESS;"
            " DEFINE TABLE licenses SCHEMALESS;"
            " DEFINE FIELD owner ON licenses TYPE option<record<persons>>"
            " REFERENCE ON DELETE CASCADE;"
        )

        await client.query("CREATE persons:alice SET name = 'Alice';")
        await client.query("CREATE licenses:lic1 SET owner = persons:alice, type = 'driving';")

        # Verify license exists
        result = await client.query("SELECT * FROM licenses;")
        assert len(result.all_records) == 1

        # Delete the person — CASCADE should delete the license
        await client.query("DELETE persons:alice;")

        # License should be gone
        result = await client.query("SELECT * FROM licenses;")
        assert len(result.all_records) == 0

    async def test_reject_prevents_deletion(self, clean_tables: None) -> None:
        """REJECT should prevent deleting a record that is referenced."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE persons SCHEMALESS;"
            " DEFINE TABLE licenses SCHEMALESS;"
            " DEFINE FIELD owner ON licenses TYPE option<record<persons>>"
            " REFERENCE ON DELETE REJECT;"
        )

        await client.query("CREATE persons:bob SET name = 'Bob';")
        await client.query("CREATE licenses:lic2 SET owner = persons:bob, type = 'medical';")

        # Trying to delete the person should fail (REJECT).
        # SurrealDB returns an ERR result in the QueryResponse (not a Python exception).
        delete_result = await client.query("DELETE persons:bob;")
        assert not delete_result.is_ok, "DELETE should fail due to REJECT"
        # Verify the error message mentions REJECT
        err_result = delete_result.first_result
        assert err_result is not None
        assert "REJECT" in str(err_result.result)

        # Both records should still exist
        result = await client.query("SELECT * FROM persons;")
        assert len(result.all_records) == 1
        result = await client.query("SELECT * FROM licenses;")
        assert len(result.all_records) == 1

    async def test_reference_with_data(self, clean_tables: None) -> None:
        """REFERENCE field should accept record references when created via SurrealQL."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query(
            "DEFINE TABLE books SCHEMALESS;"
            " DEFINE TABLE authors SCHEMALESS;"
            " DEFINE FIELD name ON authors TYPE string;"
            " DEFINE FIELD books ON authors TYPE option<array<record<books>>>"
            " REFERENCE;"
        )

        # Create books
        await client.query("CREATE books:b1 SET title = 'Book One';")
        await client.query("CREATE books:b2 SET title = 'Book Two';")

        # Create author with book references via SurrealQL
        await client.query("CREATE authors:a1 SET name = 'Jane Doe', books = [books:b1, books:b2];")

        # Query back and verify
        result = await client.query("SELECT * FROM authors WHERE name = 'Jane Doe';")
        assert len(result.all_records) == 1
        author = result.first
        assert author is not None
        assert author["name"] == "Jane Doe"
        assert len(author["books"]) == 2
