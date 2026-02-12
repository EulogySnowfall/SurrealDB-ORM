"""Unit tests for surreal_orm.migrations.migration — Migration class and helpers."""

from unittest.mock import MagicMock

import pytest

from surreal_orm.migrations.migration import (
    Migration,
    generate_migration_name,
    parse_migration_name,
)


class TestParseMigrationName:
    def test_with_py_extension(self) -> None:
        num, name = parse_migration_name("0001_initial.py")
        assert num == 1
        assert name == "initial"

    def test_without_py_extension(self) -> None:
        num, name = parse_migration_name("0042_add_users")
        assert num == 42
        assert name == "add_users"

    def test_complex_name(self) -> None:
        num, name = parse_migration_name("0010_add_email_to_users.py")
        assert num == 10
        assert name == "add_email_to_users"

    def test_invalid_no_underscore(self) -> None:
        with pytest.raises(ValueError, match="Invalid migration filename"):
            parse_migration_name("0001initial")

    def test_invalid_number(self) -> None:
        with pytest.raises(ValueError, match="Invalid migration number"):
            parse_migration_name("abc_initial")

    def test_name_with_numbers(self) -> None:
        num, name = parse_migration_name("0003_add_field_v2")
        assert num == 3
        assert name == "add_field_v2"


class TestGenerateMigrationName:
    def test_basic(self) -> None:
        assert generate_migration_name(1, "initial") == "0001_initial"

    def test_large_number(self) -> None:
        assert generate_migration_name(100, "add_users") == "0100_add_users"

    def test_spaces_to_underscores(self) -> None:
        assert generate_migration_name(1, "add users") == "0001_add_users"

    def test_hyphens_to_underscores(self) -> None:
        assert generate_migration_name(1, "fix-bug") == "0001_fix_bug"

    def test_special_chars_removed(self) -> None:
        name = generate_migration_name(1, "add @#$ stuff!")
        assert name == "0001_add__stuff"

    def test_uppercase_to_lower(self) -> None:
        assert generate_migration_name(1, "Add Users") == "0001_add_users"


class TestMigrationForwardsSQL:
    def test_empty_operations(self) -> None:
        mig = Migration(name="0001_test", operations=[])
        assert mig.forwards_sql() == []

    def test_single_operation(self) -> None:
        op = MagicMock()
        op.forwards.return_value = "CREATE TABLE users;"
        mig = Migration(name="0001_test", operations=[op])
        result = mig.forwards_sql()
        assert result == ["CREATE TABLE users;"]

    def test_multiple_operations(self) -> None:
        op1 = MagicMock()
        op1.forwards.return_value = "CREATE TABLE users;"
        op2 = MagicMock()
        op2.forwards.return_value = "DEFINE FIELD email ON users TYPE string;"
        mig = Migration(name="0001_test", operations=[op1, op2])
        result = mig.forwards_sql()
        assert len(result) == 2

    def test_skips_empty_sql(self) -> None:
        op1 = MagicMock()
        op1.forwards.return_value = "CREATE TABLE users;"
        op2 = MagicMock()
        op2.forwards.return_value = ""
        mig = Migration(name="0001_test", operations=[op1, op2])
        result = mig.forwards_sql()
        assert len(result) == 1


class TestMigrationBackwardsSQL:
    def test_empty_operations(self) -> None:
        mig = Migration(name="0001_test", operations=[])
        assert mig.backwards_sql() == []

    def test_reversible_operations(self) -> None:
        op = MagicMock()
        op.reversible = True
        op.backwards.return_value = "REMOVE TABLE users;"
        mig = Migration(name="0001_test", operations=[op])
        result = mig.backwards_sql()
        assert result == ["REMOVE TABLE users;"]

    def test_irreversible_operations_skipped(self) -> None:
        op = MagicMock()
        op.reversible = False
        mig = Migration(name="0001_test", operations=[op])
        result = mig.backwards_sql()
        assert result == []

    def test_reversed_order(self) -> None:
        op1 = MagicMock()
        op1.reversible = True
        op1.backwards.return_value = "REMOVE TABLE users;"
        op2 = MagicMock()
        op2.reversible = True
        op2.backwards.return_value = "REMOVE FIELD email ON users;"
        mig = Migration(name="0001_test", operations=[op1, op2])
        result = mig.backwards_sql()
        assert result == ["REMOVE FIELD email ON users;", "REMOVE TABLE users;"]


class TestMigrationProperties:
    def test_is_reversible_all_reversible(self) -> None:
        op1 = MagicMock(reversible=True)
        op2 = MagicMock(reversible=True)
        mig = Migration(name="0001", operations=[op1, op2])
        assert mig.is_reversible is True

    def test_is_reversible_some_irreversible(self) -> None:
        op1 = MagicMock(reversible=True)
        op2 = MagicMock(reversible=False)
        mig = Migration(name="0001", operations=[op1, op2])
        assert mig.is_reversible is False

    def test_is_reversible_empty(self) -> None:
        mig = Migration(name="0001", operations=[])
        assert mig.is_reversible is True

    def test_has_data_migrations_false(self) -> None:
        op = MagicMock()
        mig = Migration(name="0001", operations=[op])
        assert mig.has_data_migrations is False

    def test_schema_operations(self) -> None:
        op = MagicMock()
        mig = Migration(name="0001", operations=[op])
        assert mig.schema_operations == [op]

    def test_data_operations_empty(self) -> None:
        op = MagicMock()
        mig = Migration(name="0001", operations=[op])
        assert mig.data_operations == []


class TestMigrationDescribe:
    def test_describe_basic(self) -> None:
        op = MagicMock()
        op.describe.return_value = "Create table users"
        mig = Migration(name="0001_initial", operations=[op])
        desc = mig.describe()
        assert "0001_initial" in desc
        assert "Create table users" in desc
        assert "Operations (1)" in desc

    def test_describe_with_dependencies(self) -> None:
        mig = Migration(name="0002_add_fields", dependencies=["0001_initial"], operations=[])
        desc = mig.describe()
        assert "0001_initial" in desc
        assert "Dependencies" in desc

    def test_describe_no_dependencies(self) -> None:
        mig = Migration(name="0001_initial", operations=[])
        desc = mig.describe()
        assert "Dependencies" not in desc


class TestMigrationRepr:
    def test_repr(self) -> None:
        op = MagicMock()
        mig = Migration(name="0001_initial", operations=[op])
        r = repr(mig)
        assert "0001_initial" in r
        assert "operations=1" in r

    def test_repr_empty(self) -> None:
        mig = Migration(name="0001", operations=[])
        r = repr(mig)
        assert "operations=0" in r
