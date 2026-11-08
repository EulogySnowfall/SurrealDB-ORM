"""
Unit tests for #637 (v2): server-reported text written into generated model source.

``ModelCodeGenerator`` writes text it reads back from ``INFO FOR TABLE`` (a
``VALUE`` expression, a changefeed duration, an unknown table type, an HNSW
vector type, an event name and its ``WHEN`` clause, a geometry type) into
Python source. Each test parses the generated module with ``ast`` only:
nothing is imported or executed. The class body must hold exactly the
expected statements, and each literal must evaluate back to the server text.
"""

from __future__ import annotations

import ast

import pytest

from src.surreal_orm.migrations.define_parser import parse_define_field
from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
from src.surreal_orm.migrations.state import (
    EventState,
    FieldState,
    IndexState,
    SchemaState,
    TableState,
)

# Texts the server can report back for a VALUE or WHEN clause. Each one either
# closes a double-quoted literal, escapes it, or ends a source line early.
_I637_TEXTS: list[str] = [
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
    "a\u2028b\u2029c\x85d\te",
]

# Characters that end or break a source line, or that a comment should show
# as an escape rather than as is.
_I637_LINE_BREAKERS = ("\n", "\r", "\x0c", "\x00", "\x85", "\u2028", "\u2029", "\t")


def _i637_module(source: str) -> ast.Module:
    """Parse generated source; nothing is imported or executed."""
    tree = ast.parse(source)
    for node in tree.body:
        is_docstring = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        assert isinstance(node, (ast.ImportFrom, ast.ClassDef)) or is_docstring, ast.dump(node)
    return tree


def _i637_class_body(source: str, class_name: str) -> dict[str, ast.stmt]:
    """Return the class body keyed by target name, checking its shape."""
    tree = _i637_module(source)
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


def _i637_config_kwarg(body: dict[str, ast.stmt], name: str) -> object:
    config = body["model_config"]
    assert isinstance(config, ast.Assign) and isinstance(config.value, ast.Call)
    keyword = next(k for k in config.value.keywords if k.arg == name)
    return ast.literal_eval(keyword.value)


class TestI637ServerTextInGeneratedSource:
    """Server-supplied text must reach the generated module as data only."""

    def setup_method(self) -> None:
        self.gen = ModelCodeGenerator()

    @pytest.mark.parametrize("value", _I637_TEXTS)
    def test_computed_value_is_one_literal(self, value: str) -> None:
        state = SchemaState(
            tables={
                "i637_items": TableState(
                    name="i637_items",
                    fields={
                        "label": FieldState(name="label", field_type="string", nullable=False, value=value),
                    },
                )
            }
        )
        body = _i637_class_body(self.gen.generate(state), "I637Item")
        assert list(body) == ["model_config", "id", "label"]
        label = body["label"]
        assert isinstance(label, ast.AnnAssign) and isinstance(label.value, ast.Call)
        assert isinstance(label.value.func, ast.Name) and label.value.func.id == "Computed"
        assert len(label.value.args) == 1 and not label.value.keywords
        assert ast.literal_eval(label.value.args[0]) == value

    @pytest.mark.parametrize("when", _I637_TEXTS)
    def test_event_when_stays_in_one_comment_line(self, when: str) -> None:
        state = SchemaState(
            tables={
                "i637_logs": TableState(
                    name="i637_logs",
                    fields={"n": FieldState(name="n", field_type="int", nullable=False)},
                    events={"audit": EventState(name="audit", table="i637_logs", when=when, then="RETURN 1")},
                )
            }
        )
        source = self.gen.generate(state)
        body = _i637_class_body(source, "I637Log")
        assert list(body) == ["model_config", "id", "n"]
        for char in _I637_LINE_BREAKERS[1:]:
            assert char not in source
        comment_lines = [line for line in source.split("\n") if line.startswith("    # - audit: WHEN ")]
        assert len(comment_lines) == 1
        # Newlines, control characters and U+2028/U+2029 are shown as Python escapes.
        expected = when
        for char in _I637_LINE_BREAKERS:
            expected = expected.replace(char, repr(char)[1:-1])
        assert comment_lines[0] == f"    # - audit: WHEN {expected}"

    @pytest.mark.parametrize("name", ["a\nb = 1", "a\rb = 1", "a\u2028b = 1", "a\x0cb = 1"])
    def test_event_name_stays_in_comment(self, name: str) -> None:
        state = SchemaState(
            tables={
                "i637_logs": TableState(
                    name="i637_logs",
                    events={name: EventState(name=name, table="i637_logs", when="true", then="RETURN 1")},
                )
            }
        )
        source = self.gen.generate(state)
        body = _i637_class_body(source, "I637Log")
        assert list(body) == ["model_config", "id"]
        comment_lines = [line for line in source.split("\n") if line.startswith("    # - a")]
        assert comment_lines == [f"    # - {name.replace(name[1], repr(name[1])[1:-1])}: WHEN true"]

    @pytest.mark.parametrize("text", ['1h"', "1h'", "1h\\", "1h\n"])
    def test_changefeed_is_one_literal(self, text: str) -> None:
        state = SchemaState(tables={"i637_feeds": TableState(name="i637_feeds", changefeed=text)})
        body = _i637_class_body(self.gen.generate(state), "I637Feed")
        assert _i637_config_kwarg(body, "changefeed") == text

    @pytest.mark.parametrize("text", ['odd"', "odd'", "odd\\", "odd\n"])
    def test_unknown_table_type_is_one_literal(self, text: str) -> None:
        state = SchemaState(tables={"i637_kinds": TableState(name="i637_kinds", table_type=text)})
        body = _i637_class_body(self.gen.generate(state), "I637Kind")
        assert _i637_config_kwarg(body, "table_type") == text

    @pytest.mark.parametrize("text", ['F64"', "F64'", "F64\\", "F64\n"])
    def test_vector_type_is_one_literal(self, text: str) -> None:
        state = SchemaState(
            tables={
                "i637_docs": TableState(
                    name="i637_docs",
                    fields={"emb": FieldState(name="emb", field_type="array<float>", nullable=False)},
                    indexes={"emb_idx": IndexState(name="emb_idx", fields=["emb"], hnsw=True, dimension=3, vector_type=text)},
                )
            }
        )
        body = _i637_class_body(self.gen.generate(state), "I637Doc")
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
            "geometry<point> | string",
        ],
    )
    def test_geometry_type_text_stays_out_of_the_class_body(self, field_type: str) -> None:
        state = SchemaState(
            tables={
                "i637_places": TableState(
                    name="i637_places",
                    fields={"g": FieldState(name="g", field_type=field_type, nullable=False)},
                )
            }
        )
        body = _i637_class_body(self.gen.generate(state), "I637Place")
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
            ("geometry<linestring>", "linestring"),
        ],
    )
    def test_geometry_kind_is_one_literal(self, field_type: str, geo_type: str) -> None:
        field = FieldState(name="g", field_type=field_type, nullable=False)
        line, imports = self.gen._generate_field(field)
        assert line == f"g: GeoField[{geo_type!r}]"
        assert "from surreal_orm.fields import GeoField" in imports

    @pytest.mark.parametrize(
        ("definition", "geo_type"),
        [
            # INFO FOR TABLE on 2.7.0 keeps ``| null`` in the reported type.
            ("DEFINE FIELD g ON i637_places TYPE geometry<point> | null PERMISSIONS FULL", "point"),
            ("DEFINE FIELD g ON i637_places TYPE geometry<point | polygon> | null PERMISSIONS FULL", "point | polygon"),
            # 2.7.0 reports ``option<geometry<point>>`` as written.
            ("DEFINE FIELD g ON i637_places TYPE option<geometry<point>> PERMISSIONS FULL", "point"),
        ],
    )
    def test_nullable_geometry_keeps_its_kind(self, definition: str, geo_type: str) -> None:
        field = parse_define_field(definition)
        assert field.nullable
        line, imports = self.gen._generate_field(field)
        assert line == f"g: GeoField[{geo_type!r}] | None = None"
        assert "from surreal_orm.fields import GeoField" in imports

    def test_ordinary_expression_round_trips(self) -> None:
        value = "string::concat(first_name, ' ', last_name)"
        field = FieldState(name="full_name", field_type="string", nullable=False, value=value)
        line, _ = self.gen._generate_field(field)
        assert line == f"full_name: Computed[str] = Computed({value!r})"
