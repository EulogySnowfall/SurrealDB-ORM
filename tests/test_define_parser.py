"""
Unit tests for the DEFINE statement parser.

Tests parse_define_field, parse_define_table, parse_define_index,
and parse_define_access against a variety of real-world DEFINE
statement strings that SurrealDB returns from INFO commands.
"""

from __future__ import annotations

import pytest

from src.surreal_orm.migrations.define_parser import (
    parse_define_access,
    parse_define_field,
    parse_define_index,
    parse_define_table,
)
from src.surreal_orm.migrations.state import FieldState, IndexState


# ==================== parse_define_field ====================


class TestParseDefineField:
    """Tests for parse_define_field()."""

    def test_simple_string_field(self) -> None:
        result = parse_define_field("DEFINE FIELD email ON users TYPE string")
        assert result == FieldState(
            name="email",
            field_type="string",
            nullable=False,
        )

    def test_simple_int_field(self) -> None:
        result = parse_define_field("DEFINE FIELD age ON users TYPE int")
        assert result.name == "age"
        assert result.field_type == "int"
        assert result.nullable is False

    def test_optional_field(self) -> None:
        result = parse_define_field("DEFINE FIELD bio ON users TYPE option<string>")
        assert result.name == "bio"
        assert result.field_type == "string"
        assert result.nullable is True

    def test_union_null_field(self) -> None:
        result = parse_define_field("DEFINE FIELD status ON users TYPE string | null")
        assert result.nullable is True

    def test_field_with_default_string(self) -> None:
        result = parse_define_field("DEFINE FIELD role ON users TYPE string DEFAULT 'player'")
        assert result.name == "role"
        assert result.default == "player"

    def test_field_with_default_int(self) -> None:
        result = parse_define_field("DEFINE FIELD score ON users TYPE int DEFAULT 0")
        assert result.name == "score"
        assert result.default == 0

    def test_field_with_default_float(self) -> None:
        result = parse_define_field("DEFINE FIELD rating ON users TYPE float DEFAULT 1.5")
        assert result.default == 1.5

    def test_field_with_default_bool_true(self) -> None:
        result = parse_define_field("DEFINE FIELD active ON users TYPE bool DEFAULT true")
        assert result.default is True

    def test_field_with_default_bool_false(self) -> None:
        result = parse_define_field("DEFINE FIELD deleted ON users TYPE bool DEFAULT false")
        assert result.default is False

    def test_field_with_default_none(self) -> None:
        result = parse_define_field("DEFINE FIELD data ON users TYPE option<object> DEFAULT NONE")
        assert result.default is None

    def test_field_with_default_function(self) -> None:
        result = parse_define_field("DEFINE FIELD created_at ON users TYPE datetime DEFAULT time::now()")
        assert result.default == "time::now()"

    def test_field_with_assert(self) -> None:
        result = parse_define_field("DEFINE FIELD email ON users TYPE string ASSERT is::email($value)")
        assert result.assertion == "is::email($value)"

    def test_field_with_value_expression(self) -> None:
        result = parse_define_field(
            "DEFINE FIELD full_name ON users TYPE string VALUE string::concat(first_name, ' ', last_name)"
        )
        assert result.value == "string::concat(first_name, ' ', last_name)"

    def test_field_with_encrypted_value(self) -> None:
        result = parse_define_field("DEFINE FIELD password ON users TYPE string VALUE crypto::argon2::generate($value)")
        assert result.encrypted is True
        assert result.value is None  # Crypto expressions are not stored as computed values

    def test_flexible_field(self) -> None:
        result = parse_define_field("DEFINE FIELD data ON users FLEXIBLE TYPE object")
        assert result.flexible is True
        assert result.field_type == "object"

    def test_readonly_field(self) -> None:
        result = parse_define_field("DEFINE FIELD created_at ON users TYPE datetime READONLY")
        assert result.readonly is True

    def test_array_type(self) -> None:
        result = parse_define_field("DEFINE FIELD tags ON users TYPE array<string>")
        assert result.field_type == "array<string>"

    def test_record_type(self) -> None:
        result = parse_define_field("DEFINE FIELD author ON posts TYPE record<users>")
        assert result.field_type == "record<users>"

    def test_set_type(self) -> None:
        result = parse_define_field("DEFINE FIELD categories ON posts TYPE set<string>")
        assert result.field_type == "set<string>"

    def test_nested_generic_type(self) -> None:
        result = parse_define_field("DEFINE FIELD data ON events TYPE option<array<object>>")
        assert result.field_type == "array<object>"
        assert result.nullable is True

    def test_complex_field_with_all_clauses(self) -> None:
        result = parse_define_field(
            "DEFINE FIELD email ON users TYPE string DEFAULT 'unknown@example.com' ASSERT is::email($value) READONLY"
        )
        assert result.name == "email"
        assert result.field_type == "string"
        assert result.default == "unknown@example.com"
        assert result.assertion == "is::email($value)"
        assert result.readonly is True

    def test_field_with_if_not_exists(self) -> None:
        result = parse_define_field("DEFINE FIELD IF NOT EXISTS email ON users TYPE string")
        assert result.name == "email"
        assert result.field_type == "string"

    def test_field_with_overwrite(self) -> None:
        result = parse_define_field("DEFINE FIELD OVERWRITE email ON users TYPE string")
        assert result.name == "email"
        assert result.field_type == "string"

    def test_field_with_on_table_keyword(self) -> None:
        result = parse_define_field("DEFINE FIELD email ON TABLE users TYPE string")
        assert result.name == "email"
        assert result.field_type == "string"

    def test_field_with_trailing_semicolon(self) -> None:
        result = parse_define_field("DEFINE FIELD email ON users TYPE string;")
        assert result.name == "email"
        assert result.field_type == "string"

    def test_field_no_type_defaults_to_any(self) -> None:
        result = parse_define_field("DEFINE FIELD data ON users")
        assert result.name == "data"
        assert result.field_type == "any"

    def test_value_with_nested_parentheses(self) -> None:
        result = parse_define_field("DEFINE FIELD item_count ON orders TYPE int VALUE array::len(items)")
        assert result.value == "array::len(items)"

    def test_value_with_deeply_nested_functions(self) -> None:
        result = parse_define_field("DEFINE FIELD total ON orders TYPE float VALUE math::sum(items.*.price) * (1 - discount)")
        assert result.value == "math::sum(items.*.price) * (1 - discount)"

    def test_invalid_statement_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse DEFINE FIELD"):
            parse_define_field("NOT A VALID STATEMENT")

    def test_default_quoted_double(self) -> None:
        result = parse_define_field('DEFINE FIELD status ON users TYPE string DEFAULT "active"')
        assert result.default == "active"

    def test_permissions_clause_ignored(self) -> None:
        """PERMISSIONS clause is present but not stored in FieldState (no attribute)."""
        result = parse_define_field("DEFINE FIELD email ON users TYPE string PERMISSIONS FULL")
        assert result.name == "email"
        assert result.field_type == "string"


# ==================== parse_define_table ====================


class TestParseDefineTable:
    """Tests for parse_define_table()."""

    def test_schemafull_table(self) -> None:
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL")
        assert result["name"] == "users"
        assert result["schema_mode"] == "SCHEMAFULL"
        assert result["table_type"] == "normal"

    def test_schemaless_table(self) -> None:
        result = parse_define_table("DEFINE TABLE events SCHEMALESS")
        assert result["name"] == "events"
        assert result["schema_mode"] == "SCHEMALESS"

    def test_table_with_type_normal(self) -> None:
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL TYPE NORMAL")
        assert result["table_type"] == "normal"

    def test_table_with_type_relation(self) -> None:
        result = parse_define_table("DEFINE TABLE has_player SCHEMAFULL TYPE RELATION")
        assert result["table_type"] == "relation"

    def test_table_with_type_any(self) -> None:
        result = parse_define_table("DEFINE TABLE data SCHEMAFULL TYPE ANY")
        assert result["table_type"] == "any"

    def test_table_with_changefeed(self) -> None:
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL CHANGEFEED 1h")
        assert result["changefeed"] == "1h"

    def test_table_with_changefeed_include_original(self) -> None:
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL CHANGEFEED 1h INCLUDE ORIGINAL")
        assert result["changefeed"] == "1h"

    def test_table_with_permissions_full(self) -> None:
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL PERMISSIONS FULL")
        assert result["permissions"] == {}

    def test_table_with_permissions_none(self) -> None:
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL PERMISSIONS NONE")
        assert result["permissions"] == {
            "select": "NONE",
            "create": "NONE",
            "update": "NONE",
            "delete": "NONE",
        }

    def test_table_with_permissions_where(self) -> None:
        result = parse_define_table(
            "DEFINE TABLE users SCHEMAFULL PERMISSIONS FOR select WHERE $auth.id = id FOR update WHERE $auth.id = id"
        )
        assert result["permissions"]["select"] == "$auth.id = id"
        assert result["permissions"]["update"] == "$auth.id = id"

    def test_table_with_if_not_exists(self) -> None:
        result = parse_define_table("DEFINE TABLE IF NOT EXISTS users SCHEMAFULL")
        assert result["name"] == "users"

    def test_table_with_overwrite(self) -> None:
        result = parse_define_table("DEFINE TABLE OVERWRITE users SCHEMAFULL")
        assert result["name"] == "users"

    def test_table_default_schema_mode(self) -> None:
        """Table with no explicit schema mode defaults to SCHEMAFULL."""
        result = parse_define_table("DEFINE TABLE users")
        assert result["schema_mode"] == "SCHEMAFULL"

    def test_table_with_trailing_semicolon(self) -> None:
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL;")
        assert result["name"] == "users"
        assert result["schema_mode"] == "SCHEMAFULL"

    def test_table_no_changefeed(self) -> None:
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL")
        assert result["changefeed"] is None

    def test_table_with_drop(self) -> None:
        result = parse_define_table("DEFINE TABLE users DROP SCHEMAFULL")
        assert result["name"] == "users"
        assert result["schema_mode"] == "SCHEMAFULL"

    def test_invalid_statement_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse DEFINE TABLE"):
            parse_define_table("NOT A VALID STATEMENT")

    def test_table_with_relation_in_out(self) -> None:
        result = parse_define_table("DEFINE TABLE has_player SCHEMAFULL TYPE RELATION IN users OUT game_tables")
        assert result["table_type"] == "relation"


# ==================== parse_define_index ====================


class TestParseDefineIndex:
    """Tests for parse_define_index()."""

    def test_simple_index(self) -> None:
        result = parse_define_index("DEFINE INDEX idx_email ON users FIELDS email")
        assert result == IndexState(
            name="idx_email",
            fields=["email"],
            unique=False,
            search_analyzer=None,
        )

    def test_unique_index(self) -> None:
        result = parse_define_index("DEFINE INDEX idx_email ON users FIELDS email UNIQUE")
        assert result.name == "idx_email"
        assert result.fields == ["email"]
        assert result.unique is True

    def test_multi_field_index(self) -> None:
        result = parse_define_index("DEFINE INDEX idx_name ON users FIELDS first_name, last_name")
        assert result.fields == ["first_name", "last_name"]

    def test_multi_field_unique_index(self) -> None:
        result = parse_define_index("DEFINE INDEX idx_compound ON users FIELDS email, tenant_id UNIQUE")
        assert result.fields == ["email", "tenant_id"]
        assert result.unique is True

    def test_search_analyzer_index(self) -> None:
        result = parse_define_index("DEFINE INDEX idx_search ON posts FIELDS content SEARCH ANALYZER my_analyzer")
        assert result.search_analyzer == "my_analyzer"
        assert result.unique is False

    def test_columns_keyword(self) -> None:
        """COLUMNS is an alias for FIELDS in SurrealDB."""
        result = parse_define_index("DEFINE INDEX idx_email ON users COLUMNS email UNIQUE")
        assert result.fields == ["email"]
        assert result.unique is True

    def test_index_with_on_table(self) -> None:
        result = parse_define_index("DEFINE INDEX idx_email ON TABLE users FIELDS email")
        assert result.fields == ["email"]

    def test_index_with_if_not_exists(self) -> None:
        result = parse_define_index("DEFINE INDEX IF NOT EXISTS idx_email ON users FIELDS email")
        assert result.name == "idx_email"

    def test_index_with_overwrite(self) -> None:
        result = parse_define_index("DEFINE INDEX OVERWRITE idx_email ON users FIELDS email")
        assert result.name == "idx_email"

    def test_index_with_trailing_semicolon(self) -> None:
        result = parse_define_index("DEFINE INDEX idx_email ON users FIELDS email;")
        assert result.name == "idx_email"

    def test_invalid_statement_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse DEFINE INDEX"):
            parse_define_index("NOT A VALID STATEMENT")


# ==================== parse_define_access ====================


class TestParseDefineAccess:
    """Tests for parse_define_access()."""

    def test_basic_access(self) -> None:
        result = parse_define_access(
            "DEFINE ACCESS user_auth ON DATABASE TYPE RECORD "
            "SIGNUP (CREATE users SET email = $email, password = crypto::argon2::generate($password)) "
            "SIGNIN (SELECT * FROM users WHERE email = $email AND crypto::argon2::compare(password, $password)) "
            "DURATION FOR TOKEN 15m, FOR SESSION 12h"
        )
        assert result["name"] == "user_auth"
        assert result["table"] == "users"
        assert result["signup_fields"]["email"] == "$email"
        assert result["signup_fields"]["password"] == "crypto::argon2::generate($password)"
        assert "crypto::argon2::compare(password, $password)" in result["signin_where"]
        assert result["duration_token"] == "15m"
        assert result["duration_session"] == "12h"

    def test_access_with_custom_durations(self) -> None:
        result = parse_define_access(
            "DEFINE ACCESS account ON DATABASE TYPE RECORD "
            "SIGNUP (CREATE accounts SET username = $username) "
            "SIGNIN (SELECT * FROM accounts WHERE username = $username) "
            "DURATION FOR TOKEN 1h, FOR SESSION 24h"
        )
        assert result["duration_token"] == "1h"
        assert result["duration_session"] == "24h"

    def test_access_with_multiple_signup_fields(self) -> None:
        result = parse_define_access(
            "DEFINE ACCESS user_auth ON DATABASE TYPE RECORD "
            "SIGNUP (CREATE users SET email = $email, name = $name, "
            "password = crypto::argon2::generate($password)) "
            "SIGNIN (SELECT * FROM users WHERE email = $email) "
            "DURATION FOR TOKEN 15m, FOR SESSION 12h"
        )
        assert result["signup_fields"]["email"] == "$email"
        assert result["signup_fields"]["name"] == "$name"
        assert result["signup_fields"]["password"] == "crypto::argon2::generate($password)"

    def test_access_with_if_not_exists(self) -> None:
        result = parse_define_access(
            "DEFINE ACCESS IF NOT EXISTS user_auth ON DATABASE TYPE RECORD "
            "SIGNUP (CREATE users SET email = $email) "
            "SIGNIN (SELECT * FROM users WHERE email = $email) "
            "DURATION FOR TOKEN 15m, FOR SESSION 12h"
        )
        assert result["name"] == "user_auth"

    def test_access_with_overwrite(self) -> None:
        result = parse_define_access(
            "DEFINE ACCESS OVERWRITE user_auth ON DATABASE TYPE RECORD "
            "SIGNUP (CREATE users SET email = $email) "
            "SIGNIN (SELECT * FROM users WHERE email = $email) "
            "DURATION FOR TOKEN 15m, FOR SESSION 12h"
        )
        assert result["name"] == "user_auth"

    def test_access_no_durations_uses_defaults(self) -> None:
        result = parse_define_access(
            "DEFINE ACCESS user_auth ON DATABASE TYPE RECORD "
            "SIGNUP (CREATE users SET email = $email) "
            "SIGNIN (SELECT * FROM users WHERE email = $email)"
        )
        assert result["duration_token"] == "15m"
        assert result["duration_session"] == "12h"

    def test_access_with_trailing_semicolon(self) -> None:
        result = parse_define_access(
            "DEFINE ACCESS user_auth ON DATABASE TYPE RECORD "
            "SIGNUP (CREATE users SET email = $email) "
            "SIGNIN (SELECT * FROM users WHERE email = $email) "
            "DURATION FOR TOKEN 15m, FOR SESSION 12h;"
        )
        assert result["name"] == "user_auth"

    def test_invalid_statement_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse DEFINE ACCESS"):
            parse_define_access("NOT A VALID STATEMENT")

    def test_access_signin_where_with_and(self) -> None:
        result = parse_define_access(
            "DEFINE ACCESS user_auth ON DATABASE TYPE RECORD "
            "SIGNUP (CREATE users SET email = $email) "
            "SIGNIN (SELECT * FROM users WHERE email = $email AND crypto::argon2::compare(password, $password)) "
            "DURATION FOR TOKEN 15m, FOR SESSION 12h"
        )
        assert "email = $email AND crypto::argon2::compare(password, $password)" in result["signin_where"]


# ==================== Edge cases & round-trip ====================


class TestEdgeCases:
    """Edge cases and real-world DEFINE statements from SurrealDB INFO output."""

    def test_field_value_with_quoted_string_containing_comma(self) -> None:
        """VALUE expression with a string literal containing commas."""
        result = parse_define_field(
            "DEFINE FIELD greeting ON users TYPE string VALUE string::concat(first_name, ', ', 'welcome!')"
        )
        assert "string::concat" in result.value

    def test_field_type_any(self) -> None:
        result = parse_define_field("DEFINE FIELD data ON events TYPE any")
        assert result.field_type == "any"

    def test_field_type_object(self) -> None:
        result = parse_define_field("DEFINE FIELD metadata ON events TYPE object")
        assert result.field_type == "object"

    def test_field_type_number(self) -> None:
        result = parse_define_field("DEFINE FIELD amount ON orders TYPE number")
        assert result.field_type == "number"

    def test_field_type_datetime(self) -> None:
        result = parse_define_field("DEFINE FIELD created_at ON users TYPE datetime")
        assert result.field_type == "datetime"

    def test_field_type_bool(self) -> None:
        result = parse_define_field("DEFINE FIELD active ON users TYPE bool")
        assert result.field_type == "bool"

    def test_index_single_field_no_flags(self) -> None:
        result = parse_define_index("DEFINE INDEX idx_status ON orders FIELDS status")
        assert result.fields == ["status"]
        assert result.unique is False
        assert result.search_analyzer is None

    def test_multiline_field_definition(self) -> None:
        """SurrealDB may return statements on multiple lines."""
        result = parse_define_field("DEFINE FIELD email ON users\n  TYPE string\n  ASSERT is::email($value)\n  READONLY")
        assert result.field_type == "string"
        assert result.assertion == "is::email($value)"
        assert result.readonly is True

    def test_table_with_comment_clause(self) -> None:
        """COMMENT clause should be extracted but not crash the parser."""
        result = parse_define_table("DEFINE TABLE users SCHEMAFULL COMMENT 'Main user table'")
        assert result["name"] == "users"
        assert result["schema_mode"] == "SCHEMAFULL"

    def test_field_with_comment_clause(self) -> None:
        result = parse_define_field("DEFINE FIELD email ON users TYPE string COMMENT 'User email address'")
        assert result.name == "email"
        assert result.field_type == "string"
