"""
Unit tests for migration state and diff logic.
"""

from src.surreal_orm.migrations.state import (
    AccessState,
    FieldState,
    IndexState,
    SchemaState,
    TableState,
)
from src.surreal_orm.migrations.operations import (
    AddField,
    CreateIndex,
    CreateTable,
    DefineAccess,
    DropField,
    DropIndex,
    DropTable,
)


class TestFieldState:
    """Tests for FieldState dataclass."""

    def test_field_state_creation(self) -> None:
        """Test basic field state creation."""
        field = FieldState(name="email", field_type="string")
        assert field.name == "email"
        assert field.field_type == "string"
        assert field.nullable is True
        assert field.default is None
        assert field.encrypted is False

    def test_field_state_with_options(self) -> None:
        """Test field state with all options."""
        field = FieldState(
            name="password",
            field_type="string",
            nullable=False,
            default=None,
            encrypted=True,
            assertion="string::len($value) > 8",
        )
        assert field.encrypted is True
        assert field.assertion == "string::len($value) > 8"

    def test_field_state_equality(self) -> None:
        """Test field state equality comparison."""
        field1 = FieldState(name="email", field_type="string")
        field2 = FieldState(name="email", field_type="string")
        field3 = FieldState(name="email", field_type="int")

        assert field1 == field2
        assert field1 != field3

    def test_field_state_has_changed(self) -> None:
        """Test has_changed method."""
        field1 = FieldState(name="email", field_type="string")
        field2 = FieldState(name="email", field_type="string")
        field3 = FieldState(name="email", field_type="string", default="test@example.com")

        assert field1.has_changed(field2) is False
        assert field1.has_changed(field3) is True


class TestIndexState:
    """Tests for IndexState dataclass."""

    def test_index_state_creation(self) -> None:
        """Test basic index state creation."""
        index = IndexState(name="email_idx", fields=["email"])
        assert index.name == "email_idx"
        assert index.fields == ["email"]
        assert index.unique is False

    def test_index_state_unique(self) -> None:
        """Test unique index state."""
        index = IndexState(name="email_idx", fields=["email"], unique=True)
        assert index.unique is True

    def test_index_state_equality(self) -> None:
        """Test index state equality."""
        idx1 = IndexState(name="email_idx", fields=["email"], unique=True)
        idx2 = IndexState(name="email_idx", fields=["email"], unique=True)
        idx3 = IndexState(name="email_idx", fields=["email"], unique=False)

        assert idx1 == idx2
        assert idx1 != idx3


class TestAccessState:
    """Tests for AccessState dataclass."""

    def test_access_state_creation(self) -> None:
        """Test access state creation."""
        access = AccessState(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email", "password": "crypto::argon2::generate($password)"},
            signin_where="email = $email AND crypto::argon2::compare(password, $password)",
        )
        assert access.name == "user_auth"
        assert access.table == "User"
        assert access.duration_token == "15m"
        assert access.duration_session == "12h"

    def test_access_state_equality(self) -> None:
        """Test access state equality."""
        access1 = AccessState(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
        )
        access2 = AccessState(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
        )
        access3 = AccessState(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
            duration_token="1h",  # Different
        )

        assert access1 == access2
        assert access1 != access3


class TestTableState:
    """Tests for TableState dataclass."""

    def test_table_state_creation(self) -> None:
        """Test basic table state creation."""
        table = TableState(name="users")
        assert table.name == "users"
        assert table.schema_mode == "SCHEMAFULL"
        assert table.table_type == "normal"
        assert table.fields == {}
        assert table.changefeed is None

    def test_table_state_with_fields(self) -> None:
        """Test table state with fields."""
        table = TableState(
            name="users",
            fields={
                "email": FieldState(name="email", field_type="string"),
                "age": FieldState(name="age", field_type="int"),
            },
        )
        assert len(table.fields) == 2
        assert "email" in table.fields
        assert "age" in table.fields

    def test_table_state_with_access(self) -> None:
        """Test table state with access definition."""
        access = AccessState(
            name="user_auth",
            table="User",
            signup_fields={"email": "$email"},
            signin_where="email = $email",
        )
        table = TableState(name="User", table_type="user", access=access)
        assert table.access is not None
        assert table.access.name == "user_auth"


class TestSchemaState:
    """Tests for SchemaState dataclass."""

    def test_empty_schema_state(self) -> None:
        """Test empty schema state."""
        state = SchemaState()
        assert state.tables == {}
        assert state.applied_migrations == []

    def test_add_table(self) -> None:
        """Test adding a table to schema state."""
        state = SchemaState()
        table = TableState(name="users")
        state.add_table(table)
        assert "users" in state.tables

    def test_remove_table(self) -> None:
        """Test removing a table from schema state."""
        state = SchemaState()
        state.add_table(TableState(name="users"))
        state.remove_table("users")
        assert "users" not in state.tables

    def test_get_table(self) -> None:
        """Test getting a table by name."""
        state = SchemaState()
        table = TableState(name="users")
        state.add_table(table)

        assert state.get_table("users") is table
        assert state.get_table("nonexistent") is None

    def test_clone(self) -> None:
        """Test cloning schema state."""
        state = SchemaState()
        state.add_table(TableState(name="users"))
        state.applied_migrations = ["0001_initial"]

        cloned = state.clone()
        assert cloned.tables == state.tables
        assert cloned.applied_migrations == state.applied_migrations
        # Should be different objects
        assert cloned is not state


class TestSchemaStateDiff:
    """Tests for SchemaState diff logic."""

    def test_diff_empty_to_table(self) -> None:
        """Test diff from empty state to state with table."""
        current = SchemaState()
        target = SchemaState()
        target.add_table(
            TableState(
                name="users",
                fields={"email": FieldState(name="email", field_type="string")},
            )
        )

        operations = current.diff(target)

        # Should have CreateTable and AddField operations
        assert any(isinstance(op, CreateTable) and op.name == "users" for op in operations)
        assert any(isinstance(op, AddField) and op.name == "email" and op.table == "users" for op in operations)

    def test_diff_table_to_empty(self) -> None:
        """Test diff from state with table to empty state."""
        current = SchemaState()
        current.add_table(TableState(name="users"))
        target = SchemaState()

        operations = current.diff(target)

        # Should have DropTable operation
        assert any(isinstance(op, DropTable) and op.name == "users" for op in operations)

    def test_diff_add_field(self) -> None:
        """Test diff when adding a field to existing table."""
        current = SchemaState()
        current.add_table(
            TableState(
                name="users",
                fields={"email": FieldState(name="email", field_type="string")},
            )
        )

        target = SchemaState()
        target.add_table(
            TableState(
                name="users",
                fields={
                    "email": FieldState(name="email", field_type="string"),
                    "name": FieldState(name="name", field_type="string"),
                },
            )
        )

        operations = current.diff(target)

        # Should have AddField operation for 'name'
        add_ops = [op for op in operations if isinstance(op, AddField)]
        assert len(add_ops) == 1
        assert add_ops[0].name == "name"
        assert add_ops[0].table == "users"

    def test_diff_drop_field(self) -> None:
        """Test diff when dropping a field from existing table."""
        current = SchemaState()
        current.add_table(
            TableState(
                name="users",
                fields={
                    "email": FieldState(name="email", field_type="string"),
                    "old_field": FieldState(name="old_field", field_type="string"),
                },
            )
        )

        target = SchemaState()
        target.add_table(
            TableState(
                name="users",
                fields={"email": FieldState(name="email", field_type="string")},
            )
        )

        operations = current.diff(target)

        # Should have DropField operation
        drop_ops = [op for op in operations if isinstance(op, DropField)]
        assert len(drop_ops) == 1
        assert drop_ops[0].name == "old_field"

    def test_diff_add_index(self) -> None:
        """Test diff when adding an index."""
        current = SchemaState()
        current.add_table(TableState(name="users"))

        target = SchemaState()
        target.add_table(
            TableState(
                name="users",
                indexes={"email_idx": IndexState(name="email_idx", fields=["email"])},
            )
        )

        operations = current.diff(target)

        # Should have CreateIndex operation
        assert any(isinstance(op, CreateIndex) and op.name == "email_idx" for op in operations)

    def test_diff_drop_index(self) -> None:
        """Test diff when dropping an index."""
        current = SchemaState()
        current.add_table(
            TableState(
                name="users",
                indexes={"old_idx": IndexState(name="old_idx", fields=["old_field"])},
            )
        )

        target = SchemaState()
        target.add_table(TableState(name="users"))

        operations = current.diff(target)

        # Should have DropIndex operation
        assert any(isinstance(op, DropIndex) and op.name == "old_idx" for op in operations)

    def test_diff_add_access(self) -> None:
        """Test diff when adding access definition."""
        current = SchemaState()
        current.add_table(TableState(name="User", table_type="user"))

        target = SchemaState()
        target.add_table(
            TableState(
                name="User",
                table_type="user",
                access=AccessState(
                    name="user_auth",
                    table="User",
                    signup_fields={"email": "$email"},
                    signin_where="email = $email",
                ),
            )
        )

        operations = current.diff(target)

        # Should have DefineAccess operation
        assert any(isinstance(op, DefineAccess) and op.name == "user_auth" for op in operations)

    def test_diff_no_changes(self) -> None:
        """Test diff when states are identical."""
        state1 = SchemaState()
        state1.add_table(
            TableState(
                name="users",
                fields={"email": FieldState(name="email", field_type="string")},
            )
        )

        state2 = SchemaState()
        state2.add_table(
            TableState(
                name="users",
                fields={"email": FieldState(name="email", field_type="string")},
            )
        )

        operations = state1.diff(state2)

        # Should have no operations
        assert len(operations) == 0
