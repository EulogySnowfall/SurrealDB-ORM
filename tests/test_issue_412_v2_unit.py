"""
Unit tests for #412 on the v2 line: a server-reported DEFAULT that a migration file cannot re-import.

The parser reads an unquoted DEFAULT back as a ``SurrealFunc``. The plain
constructor refuses a few keyword-and-semicolon shapes meant for hand-written
values, so for a block default such as ``{ DELETE ...; RETURN 1; }`` the parser
built the object without it. Its ``repr()`` was still ``SurrealFunc('...')``,
which is what ``MigrationGenerator`` writes: importing the generated file ran
the constructor again and raised ``ValueError``, so the migration could not be
loaded (seen only when applying or rolling it back).

Run with: pytest tests/test_issue_412_v2_unit.py
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from src.surreal_orm.migrations.define_parser import parse_define_field
from src.surreal_orm.migrations.generator import MigrationGenerator
from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
from src.surreal_orm.migrations.operations import AddField, AlterField, _render_define_field
from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState
from src.surreal_orm.surreal_function import SurrealFunc, _surreal_func_from_server
from src.surreal_orm.utils import escape_single_quotes, surql_string_literal

#: ``INFO FOR TABLE`` on SurrealDB 2.7.0 for
#: ``DEFINE FIELD n ON i412 TYPE int DEFAULT { DELETE i412_log WHERE n = 0; RETURN 1; }``.
BLOCK_DEFINITION = "DEFINE FIELD n ON i412 TYPE int DEFAULT {\nDELETE i412_log WHERE n = 0;\nRETURN 1;\n} PERMISSIONS FULL"
BLOCK_EXPRESSION = "{\nDELETE i412_log WHERE n = 0;\nRETURN 1;\n}"


def _load(path: Path) -> ModuleType:
    """Import a generated migration file the way the executor does."""
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestServerDefaultRoundTrip:
    def test_parsed_block_default_is_one_the_constructor_refuses(self) -> None:
        parsed = parse_define_field(BLOCK_DEFINITION)
        assert isinstance(parsed.default, SurrealFunc)
        assert parsed.default.expression == BLOCK_EXPRESSION
        with pytest.raises(ValueError, match="dangerous"):
            SurrealFunc(BLOCK_EXPRESSION)

    def test_generated_migration_with_block_default_imports(self, tmp_path: Path) -> None:
        parsed = parse_define_field(BLOCK_DEFINITION)
        new = FieldState(name="n", field_type="int", default=parsed.default)
        old = FieldState(name="n", field_type="int", default=parsed.default, comment="before")
        operations = [
            AddField.from_field_state("i412", new),
            AlterField.from_field_states("i412", new, old),
        ]
        path = MigrationGenerator(tmp_path).generate(name="block_default", operations=operations)

        loaded = _load(path).migration.operations
        assert type(loaded[0].default).__name__ == "SurrealFunc"
        assert loaded[0].default.expression == parsed.default.expression
        assert loaded[1].default.expression == parsed.default.expression
        assert loaded[1].previous_default.expression == parsed.default.expression

    def test_loaded_block_default_renders_the_server_text(self, tmp_path: Path) -> None:
        parsed = parse_define_field(BLOCK_DEFINITION)
        op = AddField.from_field_state("i412", FieldState(name="n", field_type="int", default=parsed.default))
        path = MigrationGenerator(tmp_path).generate(name="block_render", operations=[op])
        sql = _load(path).migration.operations[0].forwards()
        assert sql.endswith(f" DEFAULT {BLOCK_EXPRESSION};")

    def test_helper_import_only_when_needed(self, tmp_path: Path) -> None:
        server = AddField(table="t", name="a", field_type="int", default=_surreal_func_from_server(BLOCK_EXPRESSION))
        source = MigrationGenerator(tmp_path)._render_migration("0001_x", [server], [])
        assert "from surreal_orm.surreal_function import _surreal_func_from_server" in source
        assert "from surreal_orm import SurrealFunc" not in source


class TestInspectdbModelsWithServerDefault:
    """``inspectdb`` writes ``repr(default)`` into a models module too."""

    @staticmethod
    def _models_source(default: SurrealFunc) -> str:
        table = TableState(name="i412_model", fields={"n": FieldState(name="n", field_type="int", default=default)})
        return ModelCodeGenerator().generate(SchemaState(tables={"i412_model": table}))

    def test_generated_models_with_block_default_execute(self) -> None:
        parsed = parse_define_field(BLOCK_DEFINITION)
        source = self._models_source(parsed.default)
        assert "from surreal_orm.surreal_function import _surreal_func_from_server" in source
        assert "from surreal_orm import SurrealFunc\n" not in source
        namespace: dict[str, object] = {}
        exec(compile(source, "<i412_models>", "exec"), namespace)
        model = namespace["I412Model"]
        default = model.model_fields["n"].default  # type: ignore[attr-defined]
        assert type(default).__name__ == "SurrealFunc"
        assert default.expression == BLOCK_EXPRESSION

    def test_generated_models_with_plain_default_unchanged(self) -> None:
        source = self._models_source(SurrealFunc("time::now()"))
        assert "from surreal_orm import SurrealFunc" in source
        assert "default=SurrealFunc('time::now()')" in source
        assert "_surreal_func_from_server" not in source


class TestOrdinaryDefaultsUnchanged:
    def test_accepted_expression_still_parses_as_plain_surrealfunc(self) -> None:
        parsed = parse_define_field("DEFINE FIELD c ON t TYPE datetime DEFAULT time::now() PERMISSIONS FULL")
        assert repr(parsed.default) == "SurrealFunc('time::now()')"

    def test_plain_surrealfunc_written_as_constructor_call(self, tmp_path: Path) -> None:
        op = AlterField.from_field_states(
            "t",
            FieldState(name="c", field_type="datetime", default=SurrealFunc("time::now()")),
            FieldState(name="c", field_type="datetime"),
        )
        source = MigrationGenerator(tmp_path)._render_migration("0002_x", [op], [])
        assert "default=SurrealFunc('time::now()')" in source
        assert "from surreal_orm import SurrealFunc" in source
        assert "_surreal_func_from_server" not in source

    def test_handwritten_refused_expression_still_raises(self) -> None:
        with pytest.raises(ValueError, match="dangerous"):
            SurrealFunc("{ DELETE x; RETURN 1; }")


class TestBothFormsCompareEqual:
    """The schema diff compares a database default with a model default."""

    def test_equal_and_same_hash(self) -> None:
        server = _surreal_func_from_server("time::now()")
        plain = SurrealFunc("time::now()")
        assert server == plain
        assert plain == server
        assert hash(server) == hash(plain)
        assert {server, plain} == {plain}

    def test_server_form_repr_is_the_helper_call(self) -> None:
        server = _surreal_func_from_server(BLOCK_EXPRESSION)
        assert repr(server) == f"_surreal_func_from_server({BLOCK_EXPRESSION!r})"
        assert isinstance(server, SurrealFunc)
        assert eval(repr(server), {"_surreal_func_from_server": _surreal_func_from_server}) == server

    def test_server_form_renders_in_ddl(self) -> None:
        sql = _render_define_field(
            "t", "n", overwrite=True, field_type="int", default=_surreal_func_from_server(BLOCK_EXPRESSION)
        )
        assert sql == f"DEFINE FIELD OVERWRITE n ON t TYPE int DEFAULT {BLOCK_EXPRESSION};"


class TestEscapeSingleQuotesDeprecated:
    def test_docstring_points_to_surql_string_literal(self) -> None:
        doc = escape_single_quotes.__doc__ or ""
        assert "deprecated" in doc.lower()
        assert "surql_string_literal" in doc

    @pytest.mark.parametrize("text", ["it's", "C:\\temp", "plain"])
    def test_behaviour_unchanged(self, text: str) -> None:
        assert escape_single_quotes(text) == surql_string_literal(text)[1:-1]
