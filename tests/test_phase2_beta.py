"""
Unit tests for v0.30.0b1 Phase 2 features:
1. Refresh token flow (AuthResult + refresh_access_token)
2. DEFINE API migration operations (with PERMISSIONS)
3. Record references field (ReferencesField with REFERENCE clause)
"""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.auth.mixins import AuthenticatedUserMixin
from src.surreal_orm.auth.result import AuthResult
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.fields.references import (
    ReferencesField,
    get_references_info,
    get_references_on_delete,
    is_references_field,
)
from src.surreal_orm.migrations.define_parser import parse_define_api, parse_define_field
from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
from src.surreal_orm.migrations.operations import AddField, DefineApi, RemoveApi
from src.surreal_orm.migrations.state import ApiState, FieldState, SchemaState, TableState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, clear_model_registry
from src.surreal_orm.types import FieldType, TableType


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear model registry before each test."""
    clear_model_registry()


# ==================== AuthResult ====================


class TestAuthResult:
    """Tests for the AuthResult dataclass."""

    def test_basic_creation(self) -> None:
        """AuthResult stores user, token, and refresh_token."""
        result = AuthResult(user="alice", token="tok123", refresh_token="ref456")
        assert result.user == "alice"
        assert result.token == "tok123"
        assert result.refresh_token == "ref456"

    def test_refresh_token_default_none(self) -> None:
        """refresh_token defaults to None (SurrealDB 2.x compat)."""
        result = AuthResult(user="alice", token="tok123")
        assert result.refresh_token is None

    def test_backward_compat_unpack_two(self) -> None:
        """Can unpack as 2-tuple for backward compatibility."""
        result = AuthResult(user="alice", token="tok123", refresh_token="ref456")
        user, token = result
        assert user == "alice"
        assert token == "tok123"

    def test_backward_compat_indexing(self) -> None:
        """Supports result[0] and result[1] indexing."""
        result = AuthResult(user="alice", token="tok123")
        assert result[0] == "alice"
        assert result[1] == "tok123"

    def test_index_out_of_range(self) -> None:
        """Raises IndexError for index >= 2."""
        result = AuthResult(user="alice", token="tok123")
        with pytest.raises(IndexError):
            _ = result[2]

    def test_len_is_two(self) -> None:
        """len() returns 2 for backward compat."""
        result = AuthResult(user="alice", token="tok123", refresh_token="ref")
        assert len(result) == 2

    def test_frozen(self) -> None:
        """AuthResult is immutable (frozen dataclass)."""
        result = AuthResult(user="alice", token="tok123")
        with pytest.raises(AttributeError):
            result.token = "new"  # type: ignore[misc]

    def test_iter_yields_user_then_token(self) -> None:
        """Iterator yields exactly (user, token)."""
        result = AuthResult(user="alice", token="tok123", refresh_token="ref")
        items = list(result)
        assert items == ["alice", "tok123"]


# ==================== refresh_access_token ====================


class TestRefreshAccessToken:
    """Tests for refresh_access_token() method."""

    def test_method_exists_and_is_async(self) -> None:
        """refresh_access_token must be an async classmethod."""
        assert hasattr(AuthenticatedUserMixin, "refresh_access_token")
        fn = AuthenticatedUserMixin.refresh_access_token.__func__  # type: ignore[attr-defined]
        assert inspect.iscoroutinefunction(fn)

    def test_return_annotation(self) -> None:
        """refresh_access_token() must return AuthResult[Self]."""
        hints = AuthenticatedUserMixin.refresh_access_token.__annotations__
        assert "return" in hints
        assert "AuthResult" in str(hints["return"])

    @pytest.mark.asyncio
    async def test_uses_signin_not_authenticate(self) -> None:
        """refresh_access_token() must use signin with refresh param, not authenticate."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_ephemeral.signin = AsyncMock(
            return_value=MagicMock(
                success=True,
                token="new_access_token",
                refresh_token="new_refresh_token",
                raw={},
            )
        )
        mock_ephemeral.close = AsyncMock()

        mock_root = AsyncMock()
        mock_user_result = MagicMock(
            is_empty=False,
            first={"id": "TestUser:1", "email": "a@b.com", "password": "$hashed"},
        )
        mock_root.query = AsyncMock(return_value=mock_user_result)

        mock_config = MagicMock()
        mock_config.namespace = "test"
        mock_config.database = "test"

        with (
            patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch.object(TestUser, "validate_token_local", return_value="TestUser:1"),
            patch(
                "src.surreal_orm.connection_manager.SurrealDBConnectionManager",
                new=MagicMock(
                    get_config=MagicMock(return_value=mock_config),
                    get_client=AsyncMock(return_value=mock_root),
                ),
            ),
        ):
            result = await TestUser.refresh_access_token("surreal-refresh-old")

            # Must use signin, NOT authenticate
            mock_ephemeral.signin.assert_called_once()
            call_kwargs = mock_ephemeral.signin.call_args
            assert call_kwargs.kwargs.get("refresh") == "surreal-refresh-old"
            assert call_kwargs.kwargs.get("access") == "testuser_auth"

            # Must NOT call authenticate
            mock_ephemeral.authenticate.assert_not_called()

            # Must return AuthResult
            assert isinstance(result, AuthResult)
            assert result.token == "new_access_token"
            assert result.refresh_token == "new_refresh_token"

    @pytest.mark.asyncio
    async def test_ephemeral_closed_on_failure(self) -> None:
        """Ephemeral connection must be closed even if refresh fails."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_ephemeral.signin = AsyncMock(return_value=MagicMock(success=False, token=None, refresh_token=None, raw="invalid"))
        mock_ephemeral.close = AsyncMock()

        mock_config = MagicMock()
        mock_config.namespace = "test"
        mock_config.database = "test"

        with (
            patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch(
                "src.surreal_orm.connection_manager.SurrealDBConnectionManager",
                new=MagicMock(get_config=MagicMock(return_value=mock_config)),
            ),
        ):
            with pytest.raises(Exception, match="Token refresh failed"):
                await TestUser.refresh_access_token("bad_refresh_token")

            mock_ephemeral.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_rotated_refresh_token(self) -> None:
        """When server rotates the refresh token, new one is returned."""

        class TestUser(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted

        mock_ephemeral = AsyncMock()
        mock_ephemeral.signin = AsyncMock(
            return_value=MagicMock(
                success=True,
                token="access_v2",
                refresh_token="refresh_v2_rotated",
                raw={},
            )
        )
        mock_ephemeral.close = AsyncMock()

        mock_root = AsyncMock()
        mock_root.query = AsyncMock(
            return_value=MagicMock(
                is_empty=False,
                first={"id": "TestUser:1", "email": "a@b.com", "password": "$h"},
            )
        )

        mock_config = MagicMock()
        mock_config.namespace = "test"
        mock_config.database = "test"

        with (
            patch.object(TestUser, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch.object(TestUser, "validate_token_local", return_value="TestUser:1"),
            patch(
                "src.surreal_orm.connection_manager.SurrealDBConnectionManager",
                new=MagicMock(
                    get_config=MagicMock(return_value=mock_config),
                    get_client=AsyncMock(return_value=mock_root),
                ),
            ),
        ):
            result = await TestUser.refresh_access_token("refresh_v1")
            assert result.refresh_token == "refresh_v2_rotated"


# ==================== DefineApi / RemoveApi ====================


class TestDefineApi:
    """Tests for the DefineApi migration operation."""

    def test_basic_forwards(self) -> None:
        """Basic DEFINE API generates correct SurrealQL."""
        op = DefineApi(name="/users/list", method="get", handler="SELECT * FROM users")
        assert op.forwards() == 'DEFINE API "/users/list" FOR get THEN { SELECT * FROM users; };'

    def test_no_method(self) -> None:
        """DEFINE API without FOR clause."""
        op = DefineApi(name="/health", handler="RETURN 'ok'")
        assert op.forwards() == """DEFINE API "/health" THEN { RETURN 'ok'; };"""

    def test_with_middleware(self) -> None:
        """DEFINE API with MIDDLEWARE clause."""
        op = DefineApi(
            name="/me",
            method="get",
            middleware=["api::timeout(1s)"],
            handler="RETURN $auth",
        )
        sql = op.forwards()
        assert "FOR get" in sql
        assert "MIDDLEWARE api::timeout(1s)" in sql
        assert "THEN { RETURN $auth; }" in sql

    def test_with_permissions(self) -> None:
        """DEFINE API with PERMISSIONS clause."""
        op = DefineApi(
            name="/admin",
            method="get",
            handler="SELECT * FROM users",
            permissions="FULL",
        )
        sql = op.forwards()
        assert "PERMISSIONS FULL" in sql

    def test_with_permissions_expression(self) -> None:
        """DEFINE API with PERMISSIONS expression."""
        op = DefineApi(
            name="/users/:id",
            method="put",
            handler="UPDATE users",
            permissions="WHERE $auth.role = 'admin'",
        )
        sql = op.forwards()
        assert "PERMISSIONS WHERE $auth.role = 'admin'" in sql

    def test_backwards(self) -> None:
        """Backwards generates REMOVE API."""
        op = DefineApi(name="/users", method="post", handler="CREATE users SET name = $name")
        assert op.backwards() == 'REMOVE API "/users";'

    def test_backwards_without_method(self) -> None:
        """Backwards generates REMOVE API without METHOD."""
        op = DefineApi(name="/health", handler="RETURN 'ok'")
        assert op.backwards() == 'REMOVE API "/health";'

    def test_describe(self) -> None:
        """describe() returns readable summary."""
        op = DefineApi(name="/users", method="get", handler="SELECT * FROM users")
        assert op.describe() == "Define API GET /users"

    def test_describe_no_method(self) -> None:
        """describe() without method."""
        op = DefineApi(name="/health", handler="RETURN 'ok'")
        assert op.describe() == "Define API /health"

    def test_method_case_normalization(self) -> None:
        """Method is lowercased in SQL output."""
        op = DefineApi(name="/users", method="POST", handler="CREATE users")
        assert "FOR post" in op.forwards()

    def test_already_quoted_name(self) -> None:
        """Name already quoted is not double-quoted."""
        op = DefineApi(name='"/users"', handler="SELECT * FROM users")
        assert op.forwards().startswith('DEFINE API "/users"')


class TestRemoveApi:
    """Tests for the RemoveApi migration operation."""

    def test_forwards(self) -> None:
        op = RemoveApi(name="/users")
        assert op.forwards() == 'REMOVE API "/users";'

    def test_not_reversible(self) -> None:
        op = RemoveApi(name="/users")
        assert op.reversible is False

    def test_describe(self) -> None:
        op = RemoveApi(name="/users", method="DELETE")
        assert op.describe() == "Remove API DELETE /users"

    def test_describe_no_method(self) -> None:
        op = RemoveApi(name="/health")
        assert op.describe() == "Remove API /health"


# ==================== ApiState ====================


class TestApiState:
    """Tests for the ApiState dataclass."""

    def test_equality(self) -> None:
        a = ApiState(name="/users", method="get", handler="SELECT * FROM users")
        b = ApiState(name="/users", method="get", handler="SELECT * FROM users")
        assert a == b

    def test_inequality_handler(self) -> None:
        a = ApiState(name="/users", method="get", handler="SELECT * FROM users")
        b = ApiState(name="/users", method="get", handler="SELECT id FROM users")
        assert a != b

    def test_inequality_method(self) -> None:
        a = ApiState(name="/users", method="get", handler="x")
        b = ApiState(name="/users", method="post", handler="x")
        assert a != b

    def test_inequality_permissions(self) -> None:
        """ApiState with different permissions are not equal."""
        a = ApiState(name="/users", method="get", handler="x", permissions="FULL")
        b = ApiState(name="/users", method="get", handler="x", permissions="NONE")
        assert a != b

    def test_inequality_middleware(self) -> None:
        """ApiState with different middleware are not equal."""
        a = ApiState(name="/users", method="get", handler="x", middleware=["api::timeout(1s)"])
        b = ApiState(name="/users", method="get", handler="x", middleware=[])
        assert a != b


# ==================== parse_define_api ====================


class TestParseDefineApi:
    """Tests for the DEFINE API parser."""

    def test_basic_parse_quoted(self) -> None:
        """Parse DEFINE API with quoted path and FOR method (SurrealDB 3.0 syntax)."""
        result = parse_define_api('DEFINE API "/users/list" FOR get THEN { SELECT * FROM users; };')
        assert result.name == "/users/list"
        assert result.method == "get"
        assert result.handler == "SELECT * FROM users"

    def test_parse_unquoted_legacy(self) -> None:
        """Parse DEFINE API with unquoted path (legacy syntax)."""
        result = parse_define_api("DEFINE API /users/list METHOD GET THEN (SELECT * FROM users);")
        assert result.name == "/users/list"
        assert result.method == "get"
        assert result.handler == "SELECT * FROM users"

    def test_parse_no_method(self) -> None:
        result = parse_define_api("""DEFINE API "/health" THEN { RETURN 'ok' }""")
        assert result.name == "/health"
        assert result.method is None
        assert result.handler == "RETURN 'ok'"

    def test_parse_if_not_exists(self) -> None:
        result = parse_define_api('DEFINE API IF NOT EXISTS "/users" FOR post THEN { CREATE users SET name = $name; }')
        assert result.name == "/users"
        assert result.method == "post"

    def test_parse_overwrite(self) -> None:
        result = parse_define_api('DEFINE API OVERWRITE "/users" FOR put THEN { UPDATE users; }')
        assert result.name == "/users"
        assert result.method == "put"

    def test_parse_with_middleware(self) -> None:
        """Parser extracts MIDDLEWARE clause."""
        result = parse_define_api('DEFINE API "/me" FOR get MIDDLEWARE api::timeout(1s) THEN { RETURN $auth; }')
        assert result.middleware == ["api::timeout(1s)"]

    def test_parse_with_permissions(self) -> None:
        """Parser extracts PERMISSIONS clause."""
        result = parse_define_api('DEFINE API "/admin" FOR get THEN { SELECT * FROM users; } PERMISSIONS FULL')
        assert result.permissions == "FULL"

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse DEFINE API"):
            parse_define_api("DEFINE TABLE users SCHEMAFULL")


# ==================== SchemaState diff for APIs ====================


class TestSchemaStateDiffApis:
    """Tests for API diff in SchemaState."""

    def test_add_api(self) -> None:
        """Diffing detects new API endpoints."""
        current = SchemaState()
        target = SchemaState(apis={"/users:get": ApiState(name="/users", method="get", handler="SELECT * FROM users")})
        ops = current.diff(target)
        define_ops = [o for o in ops if isinstance(o, DefineApi)]
        assert len(define_ops) == 1
        assert define_ops[0].name == "/users"
        assert define_ops[0].method == "get"

    def test_remove_api(self) -> None:
        """Diffing detects removed API endpoints."""
        current = SchemaState(apis={"/users:get": ApiState(name="/users", method="get", handler="SELECT * FROM users")})
        target = SchemaState()
        ops = current.diff(target)
        remove_ops = [o for o in ops if isinstance(o, RemoveApi)]
        assert len(remove_ops) == 1
        assert remove_ops[0].name == "/users"

    def test_update_api(self) -> None:
        """Diffing detects changed API handler."""
        current = SchemaState(apis={"/users:get": ApiState(name="/users", method="get", handler="SELECT * FROM users")})
        target = SchemaState(
            apis={
                "/users:get": ApiState(
                    name="/users",
                    method="get",
                    handler="SELECT id, name FROM users",
                )
            }
        )
        ops = current.diff(target)
        define_ops = [o for o in ops if isinstance(o, DefineApi)]
        assert len(define_ops) == 1
        assert "id, name" in define_ops[0].handler

    def test_no_change(self) -> None:
        """No ops generated when APIs are identical."""
        state = SchemaState(apis={"/users:get": ApiState(name="/users", method="get", handler="SELECT * FROM users")})
        ops = state.diff(state)
        api_ops = [o for o in ops if isinstance(o, (DefineApi, RemoveApi))]
        assert len(api_ops) == 0


# ==================== ReferencesField ====================


class TestReferencesField:
    """Tests for the ReferencesField type."""

    def test_is_references_field(self) -> None:
        """is_references_field() detects ReferencesField types."""
        field_type = ReferencesField["books"]
        assert is_references_field(field_type) is True

    def test_not_references_field(self) -> None:
        """is_references_field() returns False for non-references types."""
        assert is_references_field(str) is False
        assert is_references_field(list[str]) is False

    def test_get_references_info(self) -> None:
        """get_references_info() extracts the table name."""
        field_type = ReferencesField["books"]
        assert get_references_info(field_type) == "books"

    def test_get_references_info_none(self) -> None:
        """get_references_info() returns None for non-references types."""
        assert get_references_info(str) is None

    def test_different_tables(self) -> None:
        """ReferencesField works with different table names."""
        books = ReferencesField["books"]
        orders = ReferencesField["orders"]
        assert get_references_info(books) == "books"
        assert get_references_info(orders) == "orders"

    def test_on_delete_cascade(self) -> None:
        """ReferencesField with ON DELETE CASCADE."""
        field_type = ReferencesField["users", "CASCADE"]
        assert get_references_info(field_type) == "users"
        assert get_references_on_delete(field_type) == "CASCADE"

    def test_on_delete_reject(self) -> None:
        """ReferencesField with ON DELETE REJECT."""
        field_type = ReferencesField["orders", "REJECT"]
        assert get_references_on_delete(field_type) == "REJECT"

    def test_on_delete_none_by_default(self) -> None:
        """ReferencesField without ON DELETE returns None."""
        field_type = ReferencesField["books"]
        assert get_references_on_delete(field_type) is None

    def test_on_delete_case_insensitive(self) -> None:
        """ON DELETE strategy is uppercased."""
        field_type = ReferencesField["users", "cascade"]
        assert get_references_on_delete(field_type) == "CASCADE"


# ==================== REFERENCE clause in migrations ====================


class TestReferenceClauseMigrations:
    """Tests for REFERENCE clause support in AddField."""

    def test_addfield_with_reference(self) -> None:
        """AddField generates REFERENCE clause."""
        op = AddField(
            table="author",
            name="books",
            field_type="option<array<record<books>>>",
            reference=True,
        )
        sql = op.forwards()
        assert "REFERENCE" in sql
        assert sql == "DEFINE FIELD books ON author TYPE option<array<record<books>>> REFERENCE;"

    def test_addfield_with_reference_on_delete(self) -> None:
        """AddField generates REFERENCE ON DELETE CASCADE clause."""
        op = AddField(
            table="post",
            name="author",
            field_type="record<users>",
            reference=True,
            on_delete="CASCADE",
        )
        sql = op.forwards()
        assert "REFERENCE ON DELETE CASCADE" in sql

    def test_addfield_without_reference(self) -> None:
        """AddField without reference does not generate REFERENCE clause."""
        op = AddField(table="users", name="email", field_type="string")
        sql = op.forwards()
        assert "REFERENCE" not in sql

    def test_fieldstate_reference_equality(self) -> None:
        """FieldState equality considers reference and on_delete."""
        a = FieldState(name="books", field_type="array<record<books>>", reference=True)
        b = FieldState(name="books", field_type="array<record<books>>", reference=False)
        assert a != b

    def test_fieldstate_on_delete_equality(self) -> None:
        """FieldState equality considers on_delete."""
        a = FieldState(name="x", field_type="record<y>", reference=True, on_delete="CASCADE")
        b = FieldState(name="x", field_type="record<y>", reference=True, on_delete="REJECT")
        assert a != b


# ==================== parse_define_field REFERENCE ====================


class TestParseDefineFieldReference:
    """Tests for REFERENCE clause parsing."""

    def test_parse_reference(self) -> None:
        """Parser detects REFERENCE clause."""
        result = parse_define_field("DEFINE FIELD books ON author TYPE option<array<record<books>>> REFERENCE")
        assert result.reference is True
        assert result.on_delete is None

    def test_parse_reference_on_delete(self) -> None:
        """Parser detects REFERENCE ON DELETE CASCADE."""
        result = parse_define_field("DEFINE FIELD user ON post TYPE record<users> REFERENCE ON DELETE CASCADE")
        assert result.reference is True
        assert result.on_delete == "CASCADE"

    def test_parse_reference_on_delete_reject(self) -> None:
        """Parser detects REFERENCE ON DELETE REJECT."""
        result = parse_define_field("DEFINE FIELD owner ON license TYPE record<person> REFERENCE ON DELETE REJECT")
        assert result.reference is True
        assert result.on_delete == "REJECT"

    def test_parse_no_reference(self) -> None:
        """Parser sets reference=False when not present."""
        result = parse_define_field("DEFINE FIELD email ON users TYPE string")
        assert result.reference is False
        assert result.on_delete is None


# ==================== Model Generator for REFERENCE fields ====================


class TestModelGeneratorReferences:
    """Tests for model code generation with REFERENCE fields."""

    def test_generates_references_field(self) -> None:
        """Model generator produces ReferencesField for REFERENCE fields."""
        state = SchemaState(
            tables={
                "author": TableState(
                    name="author",
                    fields={
                        "name": FieldState(name="name", field_type="string", nullable=False),
                        "books": FieldState(
                            name="books",
                            field_type="array<record<books>>",
                            nullable=True,
                            reference=True,
                        ),
                    },
                ),
            }
        )
        gen = ModelCodeGenerator()
        code = gen.generate(state)
        assert 'ReferencesField["books"]' in code
        assert "from surreal_orm.fields import ReferencesField" in code

    def test_generates_references_field_with_on_delete(self) -> None:
        """Model generator produces ReferencesField with ON DELETE."""
        state = SchemaState(
            tables={
                "post": TableState(
                    name="post",
                    fields={
                        "author": FieldState(
                            name="author",
                            field_type="record<users>",
                            nullable=True,
                            reference=True,
                            on_delete="CASCADE",
                        ),
                    },
                ),
            }
        )
        gen = ModelCodeGenerator()
        code = gen.generate(state)
        assert 'ReferencesField["users", "CASCADE"]' in code


# ==================== FieldType.REFERENCES ====================


class TestFieldTypeReferences:
    """Tests for the REFERENCES field type."""

    def test_references_value(self) -> None:
        """FieldType.REFERENCES has correct string value."""
        assert FieldType.REFERENCES.value == "references"

    def test_references_generic(self) -> None:
        """FieldType.REFERENCES.generic() creates proper type string."""
        assert FieldType.REFERENCES.generic("record<books>") == "references<record<books>>"

    def test_references_from_string(self) -> None:
        """FieldType('references') resolves correctly."""
        assert FieldType("references") is FieldType.REFERENCES
