"""
Unit tests for the model code generator.

Tests ModelCodeGenerator against various SchemaState inputs to verify
that the generated Python source code is correct and importable.
"""

from __future__ import annotations

import ast

import pytest

from src.surreal_orm import SurrealFunc
from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
from src.surreal_orm.migrations.state import (
    EventState,
    FieldState,
    IndexState,
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
        # The parser reports an unquoted default as SurrealFunc (#401)
        field = FieldState(name="created_at", field_type="datetime", nullable=False, default=SurrealFunc("time::now()"))
        line, imports = self.gen._generate_field(field)
        assert "default=SurrealFunc('time::now()')" in line
        assert "from surreal_orm import SurrealFunc" in imports

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
        assert "changefeed='1h'" in source


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


# ==================== Server Text in Generated Source (#635) ====================

# Texts the server can report back for a VALUE or WHEN clause. Each one either
# closes a double-quoted literal, escapes it, or ends a source line early.
_I635_TEXTS: list[str] = [
    'string::concat(first, "-", last)',
    "string::concat(first, ' ', last)",
    'a ?? "b\\c"',
    "x\\",
    'string::replace(name, "\\"", "")',
    '"), extra = ("',
    "a\nb",
    "line\r\nnext = 1",
    "a\rb",
    "a\x0cb",
    "a\x00b",
    "a\u2028b\x85c\tc",
]


def _i635_module(source: str) -> ast.Module:
    """Parse generated source; nothing is imported or executed."""
    tree = ast.parse(source)
    for node in tree.body:
        is_docstring = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        assert isinstance(node, (ast.ImportFrom, ast.ClassDef)) or is_docstring, ast.dump(node)
    return tree


def _i635_class_body(source: str, class_name: str) -> dict[str, ast.stmt]:
    """Return the class body keyed by target name, checking its shape."""
    tree = _i635_module(source)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert [c.name for c in classes] == [class_name]
    body: dict[str, ast.stmt] = {}
    first = classes[0].body[0]
    assert isinstance(first, ast.Assign)
    assert [t.id for t in first.targets if isinstance(t, ast.Name)] == ["model_config"]
    body["model_config"] = first
    for stmt in classes[0].body[1:]:
        assert isinstance(stmt, ast.AnnAssign), ast.dump(stmt)
        assert isinstance(stmt.target, ast.Name)
        body[stmt.target.id] = stmt
    return body


def _i635_config_kwarg(body: dict[str, ast.stmt], name: str) -> object:
    config = body["model_config"]
    assert isinstance(config, ast.Assign) and isinstance(config.value, ast.Call)
    keyword = next(k for k in config.value.keywords if k.arg == name)
    return ast.literal_eval(keyword.value)


class TestI635ServerTextInGeneratedSource:
    """Server-supplied text must reach the generated module as data only."""

    def setup_method(self) -> None:
        self.gen = ModelCodeGenerator()

    @pytest.mark.parametrize("value", _I635_TEXTS)
    def test_computed_value_is_one_literal(self, value: str) -> None:
        state = SchemaState(
            tables={
                "i635_items": TableState(
                    name="i635_items",
                    fields={
                        "label": FieldState(name="label", field_type="string", nullable=False, value=value),
                    },
                )
            }
        )
        body = _i635_class_body(self.gen.generate(state), "I635Item")
        assert list(body) == ["model_config", "id", "label"]
        label = body["label"]
        assert isinstance(label, ast.AnnAssign) and isinstance(label.value, ast.Call)
        assert isinstance(label.value.func, ast.Name) and label.value.func.id == "Computed"
        assert len(label.value.args) == 1 and not label.value.keywords
        assert ast.literal_eval(label.value.args[0]) == value

    @pytest.mark.parametrize("when", _I635_TEXTS)
    def test_event_when_stays_in_one_comment_line(self, when: str) -> None:
        state = SchemaState(
            tables={
                "i635_logs": TableState(
                    name="i635_logs",
                    fields={"n": FieldState(name="n", field_type="int", nullable=False)},
                    events={"audit": EventState(name="audit", table="i635_logs", when=when, then="RETURN 1")},
                )
            }
        )
        source = self.gen.generate(state)
        body = _i635_class_body(source, "I635Log")
        assert list(body) == ["model_config", "id", "n"]
        for char in ("\r", "\x0c", "\x00", "\x85", "\u2028", "\t"):
            assert char not in source
        comment_lines = [line for line in source.split("\n") if line.startswith("    # - audit: WHEN ")]
        assert len(comment_lines) == 1
        # Newlines and control characters are shown as Python escapes.
        expected = when
        for char in ("\n", "\r", "\x0c", "\x00", "\x85", "\u2028", "\t"):
            expected = expected.replace(char, repr(char)[1:-1])
        assert comment_lines[0] == f"    # - audit: WHEN {expected}"

    def test_event_name_stays_in_comment(self) -> None:
        state = SchemaState(
            tables={
                "i635_logs": TableState(
                    name="i635_logs",
                    events={"a\nb = 1": EventState(name="a\nb = 1", table="i635_logs", when="true", then="RETURN 1")},
                )
            }
        )
        body = _i635_class_body(self.gen.generate(state), "I635Log")
        assert list(body) == ["model_config", "id"]

    @pytest.mark.parametrize("text", ['1h"', "1h'", "1h\\", "1h\n"])
    def test_changefeed_is_one_literal(self, text: str) -> None:
        state = SchemaState(tables={"i635_feeds": TableState(name="i635_feeds", changefeed=text)})
        body = _i635_class_body(self.gen.generate(state), "I635Feed")
        assert _i635_config_kwarg(body, "changefeed") == text

    @pytest.mark.parametrize("text", ['odd"', "odd'", "odd\\", "odd\n"])
    def test_unknown_table_type_is_one_literal(self, text: str) -> None:
        state = SchemaState(tables={"i635_kinds": TableState(name="i635_kinds", table_type=text)})
        body = _i635_class_body(self.gen.generate(state), "I635Kind")
        assert _i635_config_kwarg(body, "table_type") == text

    @pytest.mark.parametrize("text", ['CASCADE"', "CASCADE'", "CASCADE\\", "CASCADE\n"])
    def test_foreign_key_on_delete_is_one_literal(self, text: str) -> None:
        field = FieldState(name="owner", field_type="option<record<users>>", reference=True, on_delete=text, nullable=True)
        state = SchemaState(tables={"i635_pets": TableState(name="i635_pets", fields={"owner": field})})
        body = _i635_class_body(self.gen.generate(state), "I635Pet")
        owner = body["owner"]
        assert isinstance(owner, ast.AnnAssign) and isinstance(owner.annotation, ast.Call)
        assert [ast.literal_eval(a) for a in owner.annotation.args] == ["users"]
        keyword = next(k for k in owner.annotation.keywords if k.arg == "on_delete")
        assert ast.literal_eval(keyword.value) == text

    @pytest.mark.parametrize("text", ['CASCADE"', "CASCADE'", "CASCADE\\", "CASCADE\n"])
    def test_references_field_on_delete_is_one_literal(self, text: str) -> None:
        field = FieldState(
            name="books", field_type="option<array<record<books>>>", reference=True, on_delete=text, nullable=True
        )
        state = SchemaState(tables={"i635_shelves": TableState(name="i635_shelves", fields={"books": field})})
        body = _i635_class_body(self.gen.generate(state), "I635Shelve")
        books = body["books"]
        assert isinstance(books, ast.AnnAssign) and isinstance(books.annotation, ast.Subscript)
        assert ast.literal_eval(books.annotation.slice) == ("books", text)

    @pytest.mark.parametrize("text", ['F64"', "F64'", "F64\\", "F64\n"])
    def test_vector_type_is_one_literal(self, text: str) -> None:
        state = SchemaState(
            tables={
                "i635_docs": TableState(
                    name="i635_docs",
                    fields={"emb": FieldState(name="emb", field_type="array<float>", nullable=False)},
                    indexes={"emb_idx": IndexState(name="emb_idx", fields=["emb"], hnsw=True, dimension=3, vector_type=text)},
                )
            }
        )
        body = _i635_class_body(self.gen.generate(state), "I635Doc")
        emb = body["emb"]
        assert isinstance(emb, ast.AnnAssign) and isinstance(emb.annotation, ast.Subscript)
        assert ast.literal_eval(emb.annotation.slice) == (3, text)

    @pytest.mark.parametrize(
        "field_type",
        [
            """geometry<point> | '"]; x = (1); y: GeoField[">'""",
            """option<geometry<point> | '"]; x = (1); y: GeoField[">'>""",
            """array<geometry<point> | '"]; x = (1); y: GeoField[">'>""",
            "geometry<point'>",
            'geometry<point">',
            "geometry<point\\>",
        ],
    )
    def test_geometry_type_text_stays_out_of_the_class_body(self, field_type: str) -> None:
        state = SchemaState(
            tables={
                "i635_places": TableState(
                    name="i635_places",
                    fields={"g": FieldState(name="g", field_type=field_type, nullable=False)},
                )
            }
        )
        body = _i635_class_body(self.gen.generate(state), "I635Place")
        assert list(body) == ["model_config", "id", "g"]
        g = body["g"]
        assert isinstance(g, ast.AnnAssign)
        assert "GeoField" not in ast.unparse(g.annotation)

    @pytest.mark.parametrize(
        ("field_type", "geo_type"),
        [
            ("geometry<point>", "point"),
            ("geometry<POLYGON>", "polygon"),
            ("geometry<point | polygon>", "point | polygon"),
            ("geometry<multipolygon|multipoint>", "multipolygon | multipoint"),
            ("geometry<line>", "line"),
            ("geometry<collection>", "collection"),
        ],
    )
    def test_geometry_kind_is_one_literal(self, field_type: str, geo_type: str) -> None:
        field = FieldState(name="g", field_type=field_type, nullable=False)
        line, imports = self.gen._generate_field(field)
        assert line == f"g: GeoField[{geo_type!r}]"
        assert "from surreal_orm.fields import GeoField" in imports

    def test_ordinary_expression_round_trips(self) -> None:
        value = "string::concat(first_name, ' ', last_name)"
        field = FieldState(name="full_name", field_type="string", nullable=False, value=value)
        line, _ = self.gen._generate_field(field)
        assert line == f"full_name: Computed[str] = Computed({value!r})"
