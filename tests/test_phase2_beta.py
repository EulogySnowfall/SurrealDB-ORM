"""
Unit tests for v0.30.0b1 Phase 2 features:
1. Refresh token flow (AuthResult)
2. DEFINE API migration operations
3. Record references field (ReferencesField)
"""

import pytest

from src.surreal_orm.auth.result import AuthResult
from src.surreal_orm.fields.references import (
    ReferencesField,
    get_references_info,
    is_references_field,
)
from src.surreal_orm.migrations.define_parser import parse_define_api
from src.surreal_orm.migrations.operations import DefineApi, RemoveApi
from src.surreal_orm.migrations.state import ApiState, SchemaState
from src.surreal_orm.types import FieldType

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


# ==================== DefineApi / RemoveApi ====================


class TestDefineApi:
    """Tests for the DefineApi migration operation."""

    def test_basic_forwards(self) -> None:
        """Basic DEFINE API generates correct SurrealQL."""
        op = DefineApi(name="/users/list", method="GET", handler="SELECT * FROM users")
        assert op.forwards() == "DEFINE API /users/list METHOD GET THEN (SELECT * FROM users);"

    def test_no_method(self) -> None:
        """DEFINE API without METHOD clause."""
        op = DefineApi(name="/health", handler="RETURN 'ok'")
        assert op.forwards() == "DEFINE API /health THEN (RETURN 'ok');"

    def test_with_access(self) -> None:
        """DEFINE API with FOR access clause."""
        op = DefineApi(name="/me", method="GET", access="account", handler="RETURN $auth")
        sql = op.forwards()
        assert "METHOD GET" in sql
        assert "FOR account" in sql
        assert "THEN (RETURN $auth)" in sql

    def test_backwards_with_method(self) -> None:
        """Backwards generates REMOVE API with METHOD."""
        op = DefineApi(name="/users", method="POST", handler="CREATE users SET name = $name")
        assert op.backwards() == "REMOVE API /users METHOD POST;"

    def test_backwards_without_method(self) -> None:
        """Backwards generates REMOVE API without METHOD."""
        op = DefineApi(name="/health", handler="RETURN 'ok'")
        assert op.backwards() == "REMOVE API /health;"

    def test_describe(self) -> None:
        """describe() returns readable summary."""
        op = DefineApi(name="/users", method="GET", handler="SELECT * FROM users")
        assert op.describe() == "Define API GET /users"

    def test_describe_no_method(self) -> None:
        """describe() without method."""
        op = DefineApi(name="/health", handler="RETURN 'ok'")
        assert op.describe() == "Define API /health"

    def test_method_case_normalization(self) -> None:
        """Method is uppercased in SQL output."""
        op = DefineApi(name="/users", method="post", handler="CREATE users")
        assert "METHOD POST" in op.forwards()


class TestRemoveApi:
    """Tests for the RemoveApi migration operation."""

    def test_forwards_with_method(self) -> None:
        op = RemoveApi(name="/users", method="GET")
        assert op.forwards() == "REMOVE API /users METHOD GET;"

    def test_forwards_without_method(self) -> None:
        op = RemoveApi(name="/health")
        assert op.forwards() == "REMOVE API /health;"

    def test_not_reversible(self) -> None:
        op = RemoveApi(name="/users")
        assert op.reversible is False

    def test_describe(self) -> None:
        op = RemoveApi(name="/users", method="DELETE")
        assert op.describe() == "Remove API DELETE /users"


# ==================== ApiState ====================


class TestApiState:
    """Tests for the ApiState dataclass."""

    def test_equality(self) -> None:
        a = ApiState(name="/users", method="GET", handler="SELECT * FROM users")
        b = ApiState(name="/users", method="GET", handler="SELECT * FROM users")
        assert a == b

    def test_inequality_handler(self) -> None:
        a = ApiState(name="/users", method="GET", handler="SELECT * FROM users")
        b = ApiState(name="/users", method="GET", handler="SELECT id FROM users")
        assert a != b

    def test_inequality_method(self) -> None:
        a = ApiState(name="/users", method="GET", handler="x")
        b = ApiState(name="/users", method="POST", handler="x")
        assert a != b


# ==================== parse_define_api ====================


class TestParseDefineApi:
    """Tests for the DEFINE API parser."""

    def test_basic_parse(self) -> None:
        result = parse_define_api("DEFINE API /users/list METHOD GET THEN (SELECT * FROM users);")
        assert result.name == "/users/list"
        assert result.method == "GET"
        assert result.handler == "SELECT * FROM users"
        assert result.access is None

    def test_parse_with_access(self) -> None:
        result = parse_define_api("DEFINE API /me METHOD GET FOR account THEN (RETURN $auth)")
        assert result.name == "/me"
        assert result.method == "GET"
        assert result.access == "account"
        assert result.handler == "RETURN $auth"

    def test_parse_no_method(self) -> None:
        result = parse_define_api("DEFINE API /health THEN (RETURN 'ok')")
        assert result.name == "/health"
        assert result.method is None
        assert result.handler == "RETURN 'ok'"

    def test_parse_if_not_exists(self) -> None:
        result = parse_define_api("DEFINE API IF NOT EXISTS /users METHOD POST THEN (CREATE users SET name = $name)")
        assert result.name == "/users"
        assert result.method == "POST"

    def test_parse_overwrite(self) -> None:
        result = parse_define_api("DEFINE API OVERWRITE /users METHOD PUT THEN (UPDATE users)")
        assert result.name == "/users"
        assert result.method == "PUT"

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse DEFINE API"):
            parse_define_api("DEFINE TABLE users SCHEMAFULL")


# ==================== SchemaState diff for APIs ====================


class TestSchemaStateDiffApis:
    """Tests for API diff in SchemaState."""

    def test_add_api(self) -> None:
        """Diffing detects new API endpoints."""
        current = SchemaState()
        target = SchemaState(apis={"/users:GET": ApiState(name="/users", method="GET", handler="SELECT * FROM users")})
        ops = current.diff(target)
        define_ops = [o for o in ops if isinstance(o, DefineApi)]
        assert len(define_ops) == 1
        assert define_ops[0].name == "/users"
        assert define_ops[0].method == "GET"

    def test_remove_api(self) -> None:
        """Diffing detects removed API endpoints."""
        current = SchemaState(apis={"/users:GET": ApiState(name="/users", method="GET", handler="SELECT * FROM users")})
        target = SchemaState()
        ops = current.diff(target)
        remove_ops = [o for o in ops if isinstance(o, RemoveApi)]
        assert len(remove_ops) == 1
        assert remove_ops[0].name == "/users"

    def test_update_api(self) -> None:
        """Diffing detects changed API handler."""
        current = SchemaState(apis={"/users:GET": ApiState(name="/users", method="GET", handler="SELECT * FROM users")})
        target = SchemaState(apis={"/users:GET": ApiState(name="/users", method="GET", handler="SELECT id, name FROM users")})
        ops = current.diff(target)
        define_ops = [o for o in ops if isinstance(o, DefineApi)]
        assert len(define_ops) == 1
        assert "id, name" in define_ops[0].handler

    def test_no_change(self) -> None:
        """No ops generated when APIs are identical."""
        state = SchemaState(apis={"/users:GET": ApiState(name="/users", method="GET", handler="SELECT * FROM users")})
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
