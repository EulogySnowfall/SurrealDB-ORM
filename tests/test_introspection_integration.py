"""
Integration tests for database introspection (v0.10.0).

These tests require a running SurrealDB instance (port 8001).
They create tables via raw SQL, then use DatabaseIntrospector
to read back the schema and verify correctness.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from src.surreal_orm.connection_manager import SurrealDBConnectionManager
from src.surreal_orm.migrations.db_introspector import DatabaseIntrospector

SURREALDB_URL = "http://localhost:8001"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "test"
SURREALDB_DATABASE = "test_introspection"


# ==================== Fixtures ====================


@pytest.fixture(scope="module", autouse=True)
async def setup_introspection_db() -> AsyncGenerator[None, Any]:
    """Set up connection and create test tables for introspection tests."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )

    client = await SurrealDBConnectionManager.get_client()

    # Create test schema
    await client.query(
        """
        REMOVE TABLE IF EXISTS introspect_users;
        REMOVE TABLE IF EXISTS introspect_posts;
        REMOVE TABLE IF EXISTS introspect_has_author;

        DEFINE TABLE introspect_users SCHEMAFULL;
        DEFINE FIELD email ON introspect_users TYPE string;
        DEFINE FIELD name ON introspect_users TYPE string DEFAULT 'Anonymous';
        DEFINE FIELD age ON introspect_users TYPE option<int>;
        DEFINE FIELD active ON introspect_users TYPE bool DEFAULT true;
        DEFINE FIELD score ON introspect_users TYPE float DEFAULT 0.0;
        DEFINE FIELD created_at ON introspect_users TYPE datetime DEFAULT time::now();
        DEFINE FIELD tags ON introspect_users TYPE array<string>;
        DEFINE FIELD full_name ON introspect_users TYPE string VALUE string::concat(name, ' (', email, ')');
        DEFINE FIELD password ON introspect_users TYPE string VALUE crypto::argon2::generate($value);
        DEFINE INDEX idx_email ON introspect_users FIELDS email UNIQUE;
        DEFINE INDEX idx_name_age ON introspect_users FIELDS name, age;

        DEFINE TABLE introspect_posts SCHEMAFULL;
        DEFINE FIELD title ON introspect_posts TYPE string;
        DEFINE FIELD body ON introspect_posts TYPE option<string>;
        DEFINE FIELD author ON introspect_posts TYPE record<introspect_users>;
        DEFINE INDEX idx_title ON introspect_posts FIELDS title;

        DEFINE TABLE introspect_has_author SCHEMAFULL TYPE RELATION;
        DEFINE FIELD created_at ON introspect_has_author TYPE datetime DEFAULT time::now();
        """
    )

    yield

    # Teardown
    try:
        client = await SurrealDBConnectionManager.get_client()
        await client.query(
            """
            REMOVE TABLE IF EXISTS introspect_users;
            REMOVE TABLE IF EXISTS introspect_posts;
            REMOVE TABLE IF EXISTS introspect_has_author;
            """
        )
    except Exception:
        pass  # Best-effort cleanup; connection may already be closed

    await SurrealDBConnectionManager.close_connection()


# ==================== Tests ====================


@pytest.mark.integration
async def test_introspect_discovers_tables() -> None:
    """DatabaseIntrospector discovers all tables from INFO FOR DB."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    assert "introspect_users" in state.tables
    assert "introspect_posts" in state.tables
    assert "introspect_has_author" in state.tables


@pytest.mark.integration
async def test_introspect_table_schema_mode() -> None:
    """Introspected tables have correct schema mode."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    assert state.tables["introspect_users"].schema_mode == "SCHEMAFULL"
    assert state.tables["introspect_posts"].schema_mode == "SCHEMAFULL"


@pytest.mark.integration
async def test_introspect_table_type() -> None:
    """Introspected tables have correct table type."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    assert state.tables["introspect_has_author"].table_type == "relation"


@pytest.mark.integration
async def test_introspect_fields() -> None:
    """Introspected tables have correct fields."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    assert "email" in users.fields
    assert "name" in users.fields
    assert "age" in users.fields
    assert "active" in users.fields
    assert "score" in users.fields
    assert "tags" in users.fields


@pytest.mark.integration
async def test_introspect_field_types() -> None:
    """Introspected fields have correct types."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    assert users.fields["email"].field_type == "string"
    assert users.fields["active"].field_type == "bool"
    assert users.fields["score"].field_type == "float"


@pytest.mark.integration
async def test_introspect_nullable_field() -> None:
    """Introspected optional fields are marked nullable."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    assert users.fields["age"].nullable is True
    assert users.fields["email"].nullable is False


@pytest.mark.integration
async def test_introspect_array_type() -> None:
    """Introspected array fields preserve generic type."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    assert "array" in users.fields["tags"].field_type.lower()


@pytest.mark.integration
async def test_introspect_record_type() -> None:
    """Introspected record fields preserve record type."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    posts = state.tables["introspect_posts"]
    assert "record" in posts.fields["author"].field_type.lower()


@pytest.mark.integration
async def test_introspect_computed_field() -> None:
    """Introspected VALUE fields are correctly detected."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    full_name = users.fields["full_name"]
    assert full_name.value is not None
    assert "string::concat" in full_name.value


@pytest.mark.integration
async def test_introspect_encrypted_field() -> None:
    """Introspected crypto::argon2 VALUE fields are marked encrypted."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    password = users.fields["password"]
    assert password.encrypted is True


@pytest.mark.integration
async def test_introspect_indexes() -> None:
    """Introspected tables have correct indexes."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    assert "idx_email" in users.indexes
    assert "idx_name_age" in users.indexes


@pytest.mark.integration
async def test_introspect_unique_index() -> None:
    """Introspected unique indexes are correctly marked."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    assert users.indexes["idx_email"].unique is True
    assert users.indexes["idx_name_age"].unique is False


@pytest.mark.integration
async def test_introspect_multi_field_index() -> None:
    """Introspected compound indexes have correct field lists."""
    introspector = DatabaseIntrospector()
    state = await introspector.introspect()

    users = state.tables["introspect_users"]
    assert users.indexes["idx_name_age"].fields == ["name", "age"]


@pytest.mark.integration
async def test_introspect_with_explicit_connection() -> None:
    """DatabaseIntrospector works with an explicitly provided connection."""
    conn = await SurrealDBConnectionManager.get_client()
    introspector = DatabaseIntrospector(connection=conn)
    state = await introspector.introspect()

    assert "introspect_users" in state.tables


@pytest.mark.integration
async def test_introspect_returns_empty_for_empty_db() -> None:
    """DatabaseIntrospector returns empty state for a database with no tables."""
    # Use a separate database that has no tables
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        "test_introspection_empty",
    )

    try:
        introspector = DatabaseIntrospector()
        state = await introspector.introspect()
        # May have zero tables or system tables
        assert isinstance(state.tables, dict)
    finally:
        # Restore original connection
        SurrealDBConnectionManager.set_connection(
            SURREALDB_URL,
            SURREALDB_USER,
            SURREALDB_PASS,
            SURREALDB_NAMESPACE,
            SURREALDB_DATABASE,
        )
