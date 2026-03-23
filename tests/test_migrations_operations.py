"""
Unit tests for migration operations.
"""

import pytest

from src.surreal_orm.migrations.operations import (
    AddField,
    AlterField,
    CreateIndex,
    CreateTable,
    DataMigration,
    DefineAccess,
    DefineBearerAccess,
    DefineGraphQLConfig,
    DropField,
    DropIndex,
    DropTable,
    RawSQL,
    RebuildIndex,
    RemoveAccess,
    RemoveGraphQLConfig,
)


class TestCreateTable:
    """Tests for CreateTable operation."""

    def test_basic_create_table(self) -> None:
        """Test basic table creation."""
        op = CreateTable(name="users")
        sql = op.forwards()
        assert "DEFINE TABLE users" in sql
        assert "SCHEMAFULL" in sql

    def test_create_table_schemaless(self) -> None:
        """Test schemaless table creation."""
        op = CreateTable(name="cache", schema_mode="SCHEMALESS")
        sql = op.forwards()
        assert "SCHEMALESS" in sql
        assert "SCHEMAFULL" not in sql

    def test_create_table_with_changefeed(self) -> None:
        """Test table with changefeed."""
        op = CreateTable(name="orders", changefeed="7d")
        sql = op.forwards()
        assert "CHANGEFEED 7d" in sql

    def test_create_table_with_permissions(self) -> None:
        """Test table with permissions."""
        op = CreateTable(
            name="posts",
            permissions={"select": "true", "update": "$auth.id = author_id"},
        )
        sql = op.forwards()
        assert "PERMISSIONS" in sql
        assert "FOR select WHERE true" in sql
        assert "FOR update WHERE $auth.id = author_id" in sql

    def test_create_table_backwards(self) -> None:
        """Test table drop on rollback."""
        op = CreateTable(name="users")
        sql = op.backwards()
        assert sql == "REMOVE TABLE users;"

    def test_create_table_describe(self) -> None:
        """Test operation description."""
        op = CreateTable(name="users")
        assert op.describe() == "Create table users"

    def test_create_table_reversible(self) -> None:
        """Test that create table is reversible."""
        op = CreateTable(name="users")
        assert op.reversible is True


class TestDropTable:
    """Tests for DropTable operation."""

    def test_drop_table(self) -> None:
        """Test table drop."""
        op = DropTable(name="users")
        sql = op.forwards()
        assert sql == "REMOVE TABLE users;"

    def test_drop_table_not_reversible(self) -> None:
        """Test that drop table is not reversible."""
        op = DropTable(name="users")
        assert op.reversible is False

    def test_drop_table_backwards_empty(self) -> None:
        """Test that backwards returns empty string."""
        op = DropTable(name="users")
        assert op.backwards() == ""


class TestAddField:
    """Tests for AddField operation."""

    def test_basic_add_field(self) -> None:
        """Test basic field addition."""
        op = AddField(table="users", name="email", field_type="string")
        sql = op.forwards()
        assert "DEFINE FIELD email ON users TYPE string" in sql

    def test_add_field_with_default(self) -> None:
        """Test field with default value."""
        op = AddField(table="users", name="status", field_type="string", default="active")
        sql = op.forwards()
        assert "DEFAULT 'active'" in sql

    def test_add_field_with_function_default(self) -> None:
        """Test field with function default."""
        op = AddField(table="users", name="created_at", field_type="datetime", default="time::now()")
        sql = op.forwards()
        assert "DEFAULT time::now()" in sql

    def test_add_encrypted_field(self) -> None:
        """Test encrypted field."""
        op = AddField(table="users", name="password", field_type="string", encrypted=True)
        sql = op.forwards()
        assert "VALUE crypto::argon2::generate($value)" in sql

    def test_add_field_with_assertion(self) -> None:
        """Test field with assertion."""
        op = AddField(
            table="users",
            name="email",
            field_type="string",
            assertion="is::email($value)",
        )
        sql = op.forwards()
        assert "ASSERT is::email($value)" in sql

    def test_add_field_backwards(self) -> None:
        """Test field removal on rollback."""
        op = AddField(table="users", name="email", field_type="string")
        sql = op.backwards()
        assert sql == "REMOVE FIELD email ON users;"

    def test_add_field_describe(self) -> None:
        """Test operation description."""
        op = AddField(table="users", name="email", field_type="string")
        assert op.describe() == "Add field email to users"


class TestDropField:
    """Tests for DropField operation."""

    def test_drop_field(self) -> None:
        """Test field drop."""
        op = DropField(table="users", name="old_field")
        sql = op.forwards()
        assert sql == "REMOVE FIELD old_field ON users;"

    def test_drop_field_not_reversible(self) -> None:
        """Test that drop field is not reversible."""
        op = DropField(table="users", name="old_field")
        assert op.reversible is False


class TestAlterField:
    """Tests for AlterField operation."""

    def test_alter_field_type(self) -> None:
        """Test altering field type."""
        op = AlterField(
            table="users",
            name="age",
            field_type="int",
            previous_type="string",
        )
        sql = op.forwards()
        assert "DEFINE FIELD age ON users TYPE int" in sql

    def test_alter_field_backwards(self) -> None:
        """Test rollback restores previous type."""
        op = AlterField(
            table="users",
            name="age",
            field_type="int",
            previous_type="string",
        )
        sql = op.backwards()
        assert "TYPE string" in sql

    def test_alter_field_reversible_with_previous(self) -> None:
        """Test reversibility depends on previous_type."""
        op_reversible = AlterField(table="users", name="age", field_type="int", previous_type="string")
        assert op_reversible.reversible is True

        op_not_reversible = AlterField(table="users", name="age", field_type="int")
        assert op_not_reversible.reversible is False


class TestCreateIndex:
    """Tests for CreateIndex operation."""

    def test_basic_index(self) -> None:
        """Test basic index creation."""
        op = CreateIndex(table="users", name="email_idx", fields=["email"])
        sql = op.forwards()
        assert "DEFINE INDEX email_idx ON users FIELDS email" in sql

    def test_unique_index(self) -> None:
        """Test unique index creation."""
        op = CreateIndex(table="users", name="email_idx", fields=["email"], unique=True)
        sql = op.forwards()
        assert "UNIQUE" in sql

    def test_composite_index(self) -> None:
        """Test composite index with multiple fields."""
        op = CreateIndex(table="orders", name="user_date_idx", fields=["user_id", "created_at"])
        sql = op.forwards()
        assert "FIELDS user_id, created_at" in sql

    def test_index_backwards(self) -> None:
        """Test index removal on rollback."""
        op = CreateIndex(table="users", name="email_idx", fields=["email"])
        sql = op.backwards()
        assert sql == "REMOVE INDEX email_idx ON users;"


class TestDropIndex:
    """Tests for DropIndex operation."""

    def test_drop_index(self) -> None:
        """Test index drop."""
        op = DropIndex(table="users", name="email_idx")
        sql = op.forwards()
        assert sql == "REMOVE INDEX email_idx ON users;"

    def test_drop_index_not_reversible(self) -> None:
        """Test that drop index is not reversible."""
        op = DropIndex(table="users", name="email_idx")
        assert op.reversible is False


class TestDefineAccess:
    """Tests for DefineAccess operation."""

    def test_basic_access_definition(self) -> None:
        """Test basic access definition."""
        op = DefineAccess(
            name="user_auth",
            table="User",
            signup_fields={
                "email": "$email",
                "password": "crypto::argon2::generate($password)",
            },
            signin_where="email = $email AND crypto::argon2::compare(password, $password)",
        )
        sql = op.forwards()
        assert "DEFINE ACCESS user_auth ON DATABASE TYPE RECORD" in sql
        assert "SIGNUP (CREATE User SET" in sql
        assert "SIGNIN (SELECT * FROM User WHERE" in sql
        assert "DURATION FOR TOKEN 15m, FOR SESSION 12h" in sql

    def test_custom_durations(self) -> None:
        """Test custom token/session durations."""
        op = DefineAccess(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
            duration_token="1h",
            duration_session="24h",
        )
        sql = op.forwards()
        assert "FOR TOKEN 1h" in sql
        assert "FOR SESSION 24h" in sql

    def test_access_backwards(self) -> None:
        """Test access removal on rollback."""
        op = DefineAccess(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
        )
        sql = op.backwards()
        assert sql == "REMOVE ACCESS user_auth ON DATABASE;"


class TestRemoveAccess:
    """Tests for RemoveAccess operation."""

    def test_remove_access(self) -> None:
        """Test access removal."""
        op = RemoveAccess(name="user_auth")
        sql = op.forwards()
        assert sql == "REMOVE ACCESS user_auth ON DATABASE;"

    def test_remove_access_not_reversible(self) -> None:
        """Test that remove access is not reversible."""
        op = RemoveAccess(name="user_auth")
        assert op.reversible is False


class TestDataMigration:
    """Tests for DataMigration operation."""

    def test_data_migration_sql(self) -> None:
        """Test data migration with SQL."""
        op = DataMigration(
            forwards_sql="UPDATE users SET status = 'active' WHERE status IS NULL;",
            backwards_sql="UPDATE users SET status = NULL WHERE status = 'active';",
        )
        assert op.forwards() == "UPDATE users SET status = 'active' WHERE status IS NULL;"
        assert op.backwards() == "UPDATE users SET status = NULL WHERE status = 'active';"

    def test_data_migration_reversibility(self) -> None:
        """Test data migration reversibility based on backwards_sql."""
        op_reversible = DataMigration(
            forwards_sql="UPDATE ...",
            backwards_sql="UPDATE ...",
        )
        assert op_reversible.reversible is True

        op_not_reversible = DataMigration(forwards_sql="UPDATE ...")
        assert op_not_reversible.reversible is False

    def test_data_migration_requires_forward(self) -> None:
        """Test that data migration requires forwards_sql or forwards_func."""
        with pytest.raises(ValueError):
            DataMigration()

    def test_has_func_property(self) -> None:
        """Test has_func property."""
        op_sql = DataMigration(forwards_sql="UPDATE ...")
        assert op_sql.has_func is False

        async def migrate_func() -> None:
            pass

        op_func = DataMigration(forwards_func=migrate_func)
        assert op_func.has_func is True


class TestRawSQL:
    """Tests for RawSQL operation."""

    def test_raw_sql(self) -> None:
        """Test raw SQL execution."""
        op = RawSQL(sql="DEFINE EVENT ... ;")
        assert op.forwards() == "DEFINE EVENT ... ;"

    def test_raw_sql_reversible(self) -> None:
        """Test raw SQL reversibility."""
        op_reversible = RawSQL(sql="CREATE ...", reverse_sql="DELETE ...")
        assert op_reversible.reversible is True

        op_not_reversible = RawSQL(sql="CREATE ...")
        assert op_not_reversible.reversible is False

    def test_raw_sql_describe(self) -> None:
        """Test custom description."""
        op = RawSQL(sql="...", description="Create custom event")
        assert op.describe() == "Create custom event"


class TestRebuildIndex:
    """Tests for RebuildIndex operation."""

    def test_forwards_basic(self) -> None:
        """Test basic REBUILD INDEX generation."""
        op = RebuildIndex(table="documents", name="idx_embedding")
        assert op.forwards() == "REBUILD INDEX idx_embedding ON documents;"

    def test_forwards_if_exists(self) -> None:
        """Test REBUILD INDEX with IF EXISTS."""
        op = RebuildIndex(table="articles", name="idx_fts", if_exists=True)
        assert op.forwards() == "REBUILD INDEX IF EXISTS idx_fts ON articles;"

    def test_backwards_empty(self) -> None:
        """Test that backwards is a no-op."""
        op = RebuildIndex(table="documents", name="idx_embedding")
        assert op.backwards() == ""

    def test_not_reversible(self) -> None:
        """Test that RebuildIndex is not reversible."""
        op = RebuildIndex(table="documents", name="idx_embedding")
        assert op.reversible is False

    def test_describe(self) -> None:
        """Test human-readable description."""
        op = RebuildIndex(table="documents", name="idx_embedding")
        assert op.describe() == "Rebuild index idx_embedding on documents"


class TestDefineGraphQLConfig:
    """Tests for DefineGraphQLConfig operation."""

    def test_forwards_auto(self) -> None:
        """Test default AUTO mode."""
        op = DefineGraphQLConfig()
        assert op.forwards() == "DEFINE CONFIG GRAPHQL TABLES AUTO FUNCTIONS AUTO;"

    def test_forwards_include_tables(self) -> None:
        """Test INCLUDE mode with table list."""
        op = DefineGraphQLConfig(
            tables_mode="INCLUDE",
            tables_list=["users", "orders"],
            functions_mode="NONE",
        )
        assert op.forwards() == "DEFINE CONFIG GRAPHQL TABLES INCLUDE users, orders FUNCTIONS NONE;"

    def test_forwards_exclude_tables(self) -> None:
        """Test EXCLUDE mode with table list."""
        op = DefineGraphQLConfig(
            tables_mode="EXCLUDE",
            tables_list=["audit_log"],
        )
        assert op.forwards() == "DEFINE CONFIG GRAPHQL TABLES EXCLUDE audit_log FUNCTIONS AUTO;"

    def test_forwards_include_functions(self) -> None:
        """Test INCLUDE mode with function list."""
        op = DefineGraphQLConfig(
            functions_mode="INCLUDE",
            functions_list=["fn::get_stats", "fn::search"],
        )
        assert op.forwards() == "DEFINE CONFIG GRAPHQL TABLES AUTO FUNCTIONS INCLUDE fn::get_stats, fn::search;"

    def test_backwards(self) -> None:
        """Test backwards disables GraphQL by setting NONE modes."""
        op = DefineGraphQLConfig()
        assert op.backwards() == "DEFINE CONFIG GRAPHQL TABLES NONE FUNCTIONS NONE;"

    def test_reversible(self) -> None:
        """Test that DefineGraphQLConfig is reversible."""
        op = DefineGraphQLConfig()
        assert op.reversible is True

    def test_describe(self) -> None:
        """Test human-readable description."""
        op = DefineGraphQLConfig(tables_mode="INCLUDE", functions_mode="NONE")
        assert op.describe() == "Define GraphQL config (tables=INCLUDE, functions=NONE)"


class TestRemoveGraphQLConfig:
    """Tests for RemoveGraphQLConfig operation."""

    def test_forwards(self) -> None:
        """Test disable GraphQL config by overwriting with NONE."""
        op = RemoveGraphQLConfig()
        assert op.forwards() == "DEFINE CONFIG GRAPHQL TABLES NONE FUNCTIONS NONE;"

    def test_backwards_empty(self) -> None:
        """Test that backwards is empty."""
        op = RemoveGraphQLConfig()
        assert op.backwards() == ""

    def test_not_reversible(self) -> None:
        """Test that RemoveGraphQLConfig is not reversible."""
        op = RemoveGraphQLConfig()
        assert op.reversible is False

    def test_describe(self) -> None:
        """Test human-readable description."""
        op = RemoveGraphQLConfig()
        assert op.describe() == "Remove GraphQL config"


class TestDefineBearerAccess:
    """Tests for DefineBearerAccess operation."""

    def test_forwards_basic(self) -> None:
        """Test basic DEFINE ACCESS TYPE BEARER generation."""
        op = DefineBearerAccess(name="api_key")
        result = op.forwards()
        assert "TYPE BEARER FOR USER" in result
        assert "FOR GRANT 30d" in result
        assert "FOR SESSION 1h" in result

    def test_forwards_custom_durations(self) -> None:
        """Test custom grant and session durations."""
        op = DefineBearerAccess(
            name="service_key",
            duration_grant="90d",
            duration_session="4h",
        )
        result = op.forwards()
        assert "FOR GRANT 90d" in result
        assert "FOR SESSION 4h" in result

    def test_forwards_for_record(self) -> None:
        """Test BEARER FOR RECORD variant."""
        op = DefineBearerAccess(name="api_key", bearer_for="RECORD")
        result = op.forwards()
        assert "TYPE BEARER FOR RECORD" in result

    def test_forwards_with_comment(self) -> None:
        """Test DEFINE ACCESS TYPE BEARER with comment."""
        op = DefineBearerAccess(
            name="api_key",
            comment="Machine-to-machine API key",
        )
        result = op.forwards()
        assert "COMMENT 'Machine-to-machine API key'" in result

    def test_backwards(self) -> None:
        """Test backwards generates REMOVE ACCESS."""
        op = DefineBearerAccess(name="api_key")
        assert op.backwards() == "REMOVE ACCESS api_key ON DATABASE;"

    def test_reversible(self) -> None:
        """Test that DefineBearerAccess is reversible."""
        op = DefineBearerAccess(name="api_key")
        assert op.reversible is True

    def test_describe(self) -> None:
        """Test human-readable description."""
        op = DefineBearerAccess(name="api_key")
        assert op.describe() == "Define bearer access api_key"
