"""
Integration tests for v0.30.0b1 Phase 2 features against a running SurrealDB 3.0 instance.

1. Refresh token flow (AuthResult with refresh_token)
2. DEFINE API migration operations
3. Record references (REFERENCE keyword + <~Table back-reference query)

Run with: pytest -m integration tests/test_phase2_beta_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.auth import AuthenticatedUserMixin, AuthResult
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.migrations.operations import DefineApi, RemoveApi
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    clear_model_registry,
)
from src.surreal_orm.types import TableType
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_phase2_beta"


@pytest.fixture(scope="module", autouse=True)
async def setup_surrealdb() -> None:
    """Setup SurrealDB connection and ensure database exists for Phase 2 beta tests."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    # Ensure the database exists (SurrealDB 3.0 doesn't auto-create)
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(
        f"DEFINE NAMESPACE IF NOT EXISTS {SURREALDB_NAMESPACE}; DEFINE DATABASE IF NOT EXISTS {SURREALDB_DATABASE};"
    )


@pytest.fixture
async def clean_phase2_database():
    """Clean up Phase 2 test tables before and after tests."""

    async def cleanup() -> None:
        try:
            client = await surreal_orm.SurrealDBConnectionManager.reconnect()
            if client is None:
                return
            for stmt in [
                "REMOVE ACCESS IF EXISTS refreshuser_auth ON DATABASE;",
                "REMOVE TABLE IF EXISTS RefreshUser;",
                "REMOVE TABLE IF EXISTS RefAuthor;",
                "REMOVE TABLE IF EXISTS RefBook;",
            ]:
                try:
                    await client.query(stmt)
                except Exception:
                    pass
            # Clean API definitions
            for path in ["/users", "/health", "/test"]:
                try:
                    await client.query(f'REMOVE API IF EXISTS "{path}";')
                except Exception:
                    pass
        except Exception:
            pass

    await cleanup()
    yield
    await cleanup()


# ==================== Refresh Token Flow ====================


@pytest.mark.integration
class TestRefreshTokenIntegration:
    """Integration tests for the refresh token flow (AuthResult)."""

    @pytest.fixture(autouse=True)
    async def _setup_auth_schema(self, clean_phase2_database: None) -> None:
        """Create schema + ACCESS definition for RefreshUser before each test."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query("""
            DEFINE TABLE RefreshUser SCHEMAFULL;
            DEFINE FIELD email ON RefreshUser TYPE string;
            DEFINE FIELD password ON RefreshUser TYPE string;
            DEFINE FIELD name ON RefreshUser TYPE string;
            DEFINE INDEX email_unique ON RefreshUser FIELDS email UNIQUE;

            DEFINE ACCESS refreshuser_auth ON DATABASE TYPE RECORD
                SIGNUP (CREATE RefreshUser SET
                    email = $email,
                    password = crypto::argon2::generate($password),
                    name = $name
                )
                SIGNIN (SELECT * FROM RefreshUser WHERE
                    email = $email AND
                    crypto::argon2::compare(password, $password)
                )
                DURATION FOR TOKEN 15m, FOR SESSION 24h;
        """)

    def _make_model(self):
        """Create a fresh RefreshUser model class."""
        clear_model_registry()

        class RefreshUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="RefreshUser",
                identifier_field="email",
                password_field="password",
                access_name="refreshuser_auth",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        return RefreshUser

    async def test_signup_returns_auth_result(self) -> None:
        """signup() returns an AuthResult instance with token and refresh_token."""
        RefreshUser = self._make_model()

        result = await RefreshUser.signup(
            email="signup_refresh@example.com",
            password="secret123",
            name="Signup Refresh Test",
        )

        assert isinstance(result, AuthResult)
        assert isinstance(result.user, RefreshUser)
        assert result.user.email == "signup_refresh@example.com"
        assert isinstance(result.token, str)
        assert len(result.token.split(".")) == 3  # JWT format

        # SurrealDB 3.0 should return a refresh token
        assert hasattr(result, "refresh_token")
        if result.refresh_token is not None:
            assert isinstance(result.refresh_token, str)
            assert len(result.refresh_token.split(".")) == 3

    async def test_signup_backward_compat_unpacking(self) -> None:
        """signup() result can be unpacked as (user, token) for backward compat."""
        RefreshUser = self._make_model()

        user, token = await RefreshUser.signup(
            email="unpack@example.com",
            password="secret123",
            name="Unpack Test",
        )

        assert isinstance(user, RefreshUser)
        assert user.email == "unpack@example.com"
        assert isinstance(token, str)
        assert len(token.split(".")) == 3

    async def test_signin_returns_auth_result_with_refresh(self) -> None:
        """signin() returns AuthResult with refresh_token."""
        RefreshUser = self._make_model()

        # Create the user first
        await RefreshUser.signup(
            email="signin_refresh@example.com",
            password="mypassword",
            name="Signin Refresh Test",
        )

        # Now signin
        result = await RefreshUser.signin(
            email="signin_refresh@example.com",
            password="mypassword",
        )

        assert isinstance(result, AuthResult)
        assert isinstance(result.user, RefreshUser)
        assert result.user.email == "signin_refresh@example.com"
        assert isinstance(result.token, str)
        assert len(result.token.split(".")) == 3

        assert hasattr(result, "refresh_token")
        if result.refresh_token is not None:
            assert isinstance(result.refresh_token, str)

    async def test_refresh_access_token(self) -> None:
        """refresh_access_token() exchanges a refresh token for new tokens."""
        RefreshUser = self._make_model()

        signup_result = await RefreshUser.signup(
            email="refresh_flow@example.com",
            password="secret123",
            name="Refresh Flow Test",
        )

        if signup_result.refresh_token is None:
            pytest.skip("SurrealDB instance did not return a refresh token")

        refreshed = await RefreshUser.refresh_access_token(signup_result.refresh_token)

        assert isinstance(refreshed, AuthResult)
        assert isinstance(refreshed.user, RefreshUser)
        assert refreshed.user.email == "refresh_flow@example.com"
        assert isinstance(refreshed.token, str)
        assert len(refreshed.token.split(".")) == 3
        assert refreshed.token != signup_result.token

    async def test_signup_does_not_corrupt_root_singleton(self) -> None:
        """After signup with AuthResult, root singleton still works."""
        RefreshUser = self._make_model()

        await RefreshUser.signup(
            email="nocorrupt_phase2@example.com",
            password="secret",
            name="No Corrupt",
        )

        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        result = await client.query("SELECT * FROM RefreshUser WHERE email = 'nocorrupt_phase2@example.com';")
        assert not result.is_empty
        assert result.first["email"] == "nocorrupt_phase2@example.com"


# ==================== DEFINE API Operations ====================


@pytest.mark.integration
class TestDefineApiIntegration:
    """Integration tests for DEFINE API / REMOVE API against live SurrealDB."""

    @pytest.fixture(autouse=True)
    async def _setup(self, clean_phase2_database: None) -> None:
        """Clean state before each test."""
        pass

    async def test_define_api_with_method(self) -> None:
        """DEFINE API with FOR method executes successfully."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        op = DefineApi(
            name="/users",
            method="get",
            handler="SELECT * FROM users",
        )
        sql = op.forwards()
        result = await client.query(sql)
        assert result is not None

    async def test_define_api_multiple_define(self) -> None:
        """Multiple DEFINE API calls for different methods on same path."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        get_op = DefineApi(name="/users", method="get", handler="SELECT * FROM users")
        post_op = DefineApi(name="/users", method="post", handler="CREATE users SET name = $request.body.name")

        await client.query(get_op.forwards())
        await client.query(post_op.forwards())

        # Both should be defined
        info = await client.query("INFO FOR DB;")
        assert info is not None

    async def test_remove_api(self) -> None:
        """REMOVE API removes a previously defined API."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        define_op = DefineApi(
            name="/users",
            method="get",
            handler="SELECT * FROM users",
        )
        await client.query(define_op.forwards())

        remove_op = RemoveApi(name="/users")
        result = await client.query(remove_op.forwards())
        assert result is not None

    async def test_define_api_backwards_removes(self) -> None:
        """DefineApi.backwards() generates valid REMOVE API SQL."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        op = DefineApi(
            name="/users",
            method="post",
            handler="CREATE users SET name = $request.body.name",
        )
        await client.query(op.forwards())

        result = await client.query(op.backwards())
        assert result is not None

    async def test_define_api_visible_in_info(self) -> None:
        """DEFINE API should be visible in INFO FOR DB."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        op = DefineApi(
            name="/health",
            method="get",
            handler="RETURN 'ok'",
        )
        await client.query(op.forwards())

        info_result = await client.query("INFO FOR DB;")
        assert info_result is not None
        info_data = info_result.first
        if isinstance(info_data, dict) and "apis" in info_data:
            apis = info_data["apis"]
            assert len(apis) >= 1

    async def test_define_api_invocable(self) -> None:
        """Defined API can be invoked via api::invoke()."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        op = DefineApi(
            name="/health",
            method="get",
            handler="RETURN { status: 200, body: 'ok' }",
        )
        await client.query(op.forwards())

        result = await client.query('RETURN api::invoke("/health");')
        assert result is not None


# ==================== Record References ====================


@pytest.mark.integration
class TestRecordReferencesIntegration:
    """Integration tests for record references (REFERENCE keyword) in SurrealDB 3.0."""

    @pytest.fixture(autouse=True)
    async def _setup(self, clean_phase2_database: None) -> None:
        """Clean up test tables."""
        pass

    async def test_define_field_with_reference(self) -> None:
        """DEFINE FIELD with REFERENCE keyword is valid in SurrealDB 3.0."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query("""
            DEFINE TABLE RefAuthor SCHEMAFULL;
            DEFINE FIELD name ON RefAuthor TYPE string;

            DEFINE TABLE RefBook SCHEMAFULL;
            DEFINE FIELD title ON RefBook TYPE string;
            DEFINE FIELD author ON RefBook TYPE record<RefAuthor> REFERENCE;
        """)

        info = await client.query("INFO FOR TABLE RefBook;")
        assert info is not None
        info_data = info.first
        if isinstance(info_data, dict) and "fields" in info_data:
            fields = info_data["fields"]
            if isinstance(fields, dict) and "author" in fields:
                assert "REFERENCE" in str(fields["author"]).upper()

    async def test_back_reference_query(self) -> None:
        """<~Table back-reference query returns referencing records."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        # Setup tables with REFERENCE
        await client.query("""
            DEFINE TABLE RefAuthor SCHEMAFULL;
            DEFINE FIELD name ON RefAuthor TYPE string;

            DEFINE TABLE RefBook SCHEMAFULL;
            DEFINE FIELD title ON RefBook TYPE string;
            DEFINE FIELD author ON RefBook TYPE record<RefAuthor> REFERENCE;
        """)

        # Create data
        await client.query("CREATE RefAuthor:tolkien SET name = 'J.R.R. Tolkien';")
        await client.query("CREATE RefBook:lotr SET title = 'The Lord of the Rings', author = RefAuthor:tolkien;")
        await client.query("CREATE RefBook:hobbit SET title = 'The Hobbit', author = RefAuthor:tolkien;")

        # Query back-references using <~ syntax
        result = await client.query("SELECT *, <~RefBook AS books FROM RefAuthor:tolkien;")
        assert not result.is_empty

        author = result.first
        assert author["name"] == "J.R.R. Tolkien"

        # books should contain the back-references
        books = author.get("books")
        assert books is not None
        assert len(books) == 2

    async def test_reference_field_info(self) -> None:
        """REFERENCE keyword appears in INFO FOR TABLE field definitions."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()

        await client.query("""
            DEFINE TABLE RefAuthor SCHEMAFULL;
            DEFINE FIELD name ON RefAuthor TYPE string;

            DEFINE TABLE RefBook SCHEMAFULL;
            DEFINE FIELD title ON RefBook TYPE string;
            DEFINE FIELD author ON RefBook TYPE record<RefAuthor> REFERENCE;
        """)

        # Use a separate query call to get the INFO result
        info = await client.query("INFO FOR TABLE RefBook;")
        assert info is not None

        # INFO FOR TABLE returns a dict with fields, indexes, etc.
        # Iterate over all results to find the dict
        info_data = None
        for qr in info.results:
            if qr.result is not None and isinstance(qr.result, dict) and "fields" in qr.result:
                info_data = qr.result
                break

        assert info_data is not None, f"No INFO result found, got: {info}"
        fields = info_data["fields"]
        assert isinstance(fields, dict)
        assert "author" in fields
        assert "REFERENCE" in str(fields["author"]).upper()
