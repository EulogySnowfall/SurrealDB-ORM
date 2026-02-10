"""
Unit tests for the model code generator.

Tests ModelCodeGenerator against various SchemaState inputs to verify
that the generated Python source code is correct and importable.
"""

from __future__ import annotations

import pytest

from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
from src.surreal_orm.migrations.state import (
    FieldState,
    SchemaState,
    TableState,
)


@pytest.fixture
def generator() -> ModelCodeGenerator:
    return ModelCodeGenerator()


# ==================== Table → Class Name ====================


class TestTableToClassName:
    def test_simple_plural(self) -> None:
        assert ModelCodeGenerator._table_to_class_name("users") == "User"

    def test_simple_singular(self) -> None:
        assert ModelCodeGenerator._table_to_class_name("user") == "User"

    def test_snake_case_plural(self) -> None:
        assert ModelCodeGenerator._table_to_class_name("game_tables") == "GameTable"

    def test_snake_case_singular(self) -> None:
        assert ModelCodeGenerator._table_to_class_name("game_table") == "GameTable"

    def test_relation_table(self) -> None:
        assert ModelCodeGenerator._table_to_class_name("has_player") == "HasPlayer"

    def test_single_char_no_strip(self) -> None:
        # "s" alone should not be stripped to empty
        assert ModelCodeGenerator._table_to_class_name("s") == "S"


# ==================== SurrealDB Type → Python Type ====================


class TestSurrealTypeToPython:
    def setup_method(self) -> None:
        self.gen = ModelCodeGenerator()

    def test_string(self) -> None:
        assert self.gen._surreal_type_to_python("string") == "str"

    def test_int(self) -> None:
        assert self.gen._surreal_type_to_python("int") == "int"

    def test_float(self) -> None:
        assert self.gen._surreal_type_to_python("float") == "float"

    def test_bool(self) -> None:
        assert self.gen._surreal_type_to_python("bool") == "bool"

    def test_datetime(self) -> None:
        assert self.gen._surreal_type_to_python("datetime") == "datetime"

    def test_object(self) -> None:
        assert self.gen._surreal_type_to_python("object") == "dict[str, Any]"

    def test_any(self) -> None:
        assert self.gen._surreal_type_to_python("any") == "Any"

    def test_option_string(self) -> None:
        result = self.gen._surreal_type_to_python("option<string>")
        assert result == "str | None"

    def test_option_int(self) -> None:
        result = self.gen._surreal_type_to_python("option<int>")
        assert result == "int | None"

    def test_array_string(self) -> None:
        result = self.gen._surreal_type_to_python("array<string>")
        assert result == "list[str]"

    def test_array_int(self) -> None:
        result = self.gen._surreal_type_to_python("array<int>")
        assert result == "list[int]"

    def test_set_string(self) -> None:
        result = self.gen._surreal_type_to_python("set<string>")
        assert result == "set[str]"

    def test_record(self) -> None:
        result = self.gen._surreal_type_to_python("record<users>")
        assert result == "str"

    def test_unknown_type_defaults_to_any(self) -> None:
        result = self.gen._surreal_type_to_python("custom_type")
        assert result == "Any"

    def test_decimal(self) -> None:
        assert self.gen._surreal_type_to_python("decimal") == "Decimal"

    def test_number(self) -> None:
        assert self.gen._surreal_type_to_python("number") == "float"

    def test_bytes(self) -> None:
        assert self.gen._surreal_type_to_python("bytes") == "bytes"


# ==================== Field Generation ====================


class TestGenerateField:
    def setup_method(self) -> None:
        self.gen = ModelCodeGenerator()

    def test_simple_string_field(self) -> None:
        field = FieldState(name="email", field_type="string", nullable=False)
        line, imports = self.gen._generate_field(field)
        assert line == "email: str"

    def test_nullable_field(self) -> None:
        field = FieldState(name="bio", field_type="string", nullable=True)
        line, imports = self.gen._generate_field(field)
        assert line == "bio: str | None = None"

    def test_field_with_default_string(self) -> None:
        field = FieldState(name="role", field_type="string", nullable=False, default="player")
        line, imports = self.gen._generate_field(field)
        assert "default='player'" in line

    def test_field_with_default_int(self) -> None:
        field = FieldState(name="score", field_type="int", nullable=False, default=0)
        line, imports = self.gen._generate_field(field)
        assert "default=0" in line

    def test_field_with_default_bool(self) -> None:
        field = FieldState(name="active", field_type="bool", nullable=False, default=True)
        line, imports = self.gen._generate_field(field)
        assert "default=True" in line

    def test_field_with_default_function(self) -> None:
        field = FieldState(name="created_at", field_type="datetime", nullable=False, default="time::now()")
        line, imports = self.gen._generate_field(field)
        assert '"time::now()"' in line

    def test_computed_field(self) -> None:
        field = FieldState(
            name="full_name",
            field_type="string",
            nullable=False,
            value="string::concat(first_name, ' ', last_name)",
        )
        line, imports = self.gen._generate_field(field)
        assert "Computed[str]" in line
        assert "Computed(" in line
        assert "string::concat" in line
        assert any("Computed" in imp for imp in imports)

    def test_encrypted_field(self) -> None:
        field = FieldState(name="password", field_type="string", nullable=False, encrypted=True)
        line, imports = self.gen._generate_field(field)
        assert line == "password: Encrypted"
        assert any("Encrypted" in imp for imp in imports)

    def test_array_field(self) -> None:
        field = FieldState(name="tags", field_type="array<string>", nullable=False)
        line, imports = self.gen._generate_field(field)
        assert "list[str]" in line


# ==================== Full Model Generation ====================


class TestGenerateModelClass:
    def setup_method(self) -> None:
        self.gen = ModelCodeGenerator()

    def test_basic_model(self) -> None:
        table = TableState(
            name="users",
            schema_mode="SCHEMAFULL",
            table_type="normal",
            fields={
                "email": FieldState(name="email", field_type="string", nullable=False),
                "age": FieldState(name="age", field_type="int", nullable=False),
            },
        )
        source, _ = self.gen._generate_model_class(table)
        assert "class User(BaseSurrealModel):" in source
        assert 'table_name="users"' in source
        assert "id: str | None = None" in source
        assert "age: int" in source
        assert "email: str" in source

    def test_relation_table(self) -> None:
        table = TableState(
            name="has_player",
            schema_mode="SCHEMAFULL",
            table_type="relation",
        )
        source, imports = self.gen._generate_model_class(table)
        assert "class HasPlayer(BaseSurrealModel):" in source
        assert "TableType.RELATION" in source
        assert any("TableType" in imp for imp in imports)

    def test_table_with_changefeed(self) -> None:
        table = TableState(
            name="events",
            schema_mode="SCHEMAFULL",
            table_type="normal",
            changefeed="1h",
        )
        source, _ = self.gen._generate_model_class(table)
        assert 'changefeed="1h"' in source


# ==================== Full Module Generation ====================


class TestGenerateModule:
    def setup_method(self) -> None:
        self.gen = ModelCodeGenerator()

    def test_empty_schema(self) -> None:
        state = SchemaState()
        source = self.gen.generate(state)
        assert "Auto-generated models" in source
        assert "from surreal_orm import BaseSurrealModel" in source

    def test_single_table(self) -> None:
        state = SchemaState(
            tables={
                "users": TableState(
                    name="users",
                    schema_mode="SCHEMAFULL",
                    table_type="normal",
                    fields={
                        "email": FieldState(name="email", field_type="string", nullable=False),
                        "name": FieldState(name="name", field_type="string", nullable=False, default="Anonymous"),
                    },
                )
            }
        )
        source = self.gen.generate(state)
        assert "class User(BaseSurrealModel):" in source
        assert "email: str" in source
        assert "'Anonymous'" in source

    def test_multiple_tables(self) -> None:
        state = SchemaState(
            tables={
                "users": TableState(name="users", fields={}),
                "posts": TableState(name="posts", fields={}),
            }
        )
        source = self.gen.generate(state)
        assert "class User(" in source
        assert "class Post(" in source

    def test_generated_code_has_future_annotations(self) -> None:
        state = SchemaState(
            tables={
                "users": TableState(name="users", fields={}),
            }
        )
        source = self.gen.generate(state)
        assert "from __future__ import annotations" in source

    def test_generated_code_includes_required_imports(self) -> None:
        state = SchemaState(
            tables={
                "users": TableState(
                    name="users",
                    fields={
                        "created_at": FieldState(name="created_at", field_type="datetime", nullable=False),
                    },
                )
            }
        )
        source = self.gen.generate(state)
        assert "from datetime import datetime" in source

    def test_encrypted_field_imports(self) -> None:
        state = SchemaState(
            tables={
                "users": TableState(
                    name="users",
                    fields={
                        "password": FieldState(name="password", field_type="string", encrypted=True),
                    },
                )
            }
        )
        source = self.gen.generate(state)
        assert "from surreal_orm.fields import Encrypted" in source

    def test_computed_field_imports(self) -> None:
        state = SchemaState(
            tables={
                "users": TableState(
                    name="users",
                    fields={
                        "full_name": FieldState(
                            name="full_name",
                            field_type="string",
                            value="string::concat(first_name, ' ', last_name)",
                        ),
                    },
                )
            }
        )
        source = self.gen.generate(state)
        assert "from surreal_orm.fields import Computed" in source


# ==================== Import Sorting ====================


class TestSortImports:
    def test_future_comes_first(self) -> None:
        imports = {
            "from surreal_orm import BaseSurrealModel",
            "from __future__ import annotations",
            "from datetime import datetime",
        }
        result = ModelCodeGenerator._sort_imports(imports)
        assert result[0] == "from __future__ import annotations"

    def test_stdlib_before_third_party(self) -> None:
        imports = {
            "from surreal_orm import BaseSurrealModel",
            "from datetime import datetime",
        }
        result = ModelCodeGenerator._sort_imports(imports)
        stdlib_idx = next(i for i, line in enumerate(result) if "datetime" in line)
        third_idx = next(i for i, line in enumerate(result) if "surreal_orm" in line)
        assert stdlib_idx < third_idx

    def test_empty_strings_skipped(self) -> None:
        imports = {"", "from datetime import datetime"}
        result = ModelCodeGenerator._sort_imports(imports)
        assert all(line.strip() or line == "" for line in result)
