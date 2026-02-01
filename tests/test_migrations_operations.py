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
    DropField,
    DropIndex,
    DropTable,
    RawSQL,
    RemoveAccess,
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
