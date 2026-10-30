"""Unit tests for v0.36.0 Graph Power: recursive traversal, paths and shortest path.

No server: the helpers of ``surreal_orm.graph`` are pure, and the QuerySet
compilers are checked on their emitted text and bound variables.

``RecordId`` is imported from ``surreal_sdk.protocol.cbor``, never from
``src.surreal_sdk.protocol.cbor``: with ``PYTHONPATH=.`` the latter is a second,
distinct class, which the ORM does not recognise and which never compares equal
to the ORM's own.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.surreal_orm import graph
from src.surreal_orm.fields.relation import ForeignKey
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.q import Q
from src.surreal_orm.query_set import QuerySet
from src.surreal_orm.subquery import Subquery
from src.surreal_orm.types import TableType
from src.surreal_orm.utils import parse_record_id
from surreal_sdk.protocol.cbor import RecordId


class GPUPerson(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="gpu_person")

    id: str | None = None
    name: str = ""
    active: bool | None = None


class GPUKnows(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="gpu_knows", table_type=TableType.RELATION)

    id: str | None = None


class GPUCity(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="gpu_city")

    id: str | None = None


class GPUWithFK(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="gpu_with_fk")

    id: str | None = None
    owner: ForeignKey("GPUPerson") = None  # type: ignore[valid-type]


def r(key: str) -> RecordId:
    return RecordId("gpu_person", key)


E18 = "The ⟨…⟩ record-id spelling is not supported"


# ---------------------------------------------------------------------------
# U1 — graph.py helpers
# ---------------------------------------------------------------------------


class TestNoSecondIdReader:
    def test_no_split_graph_ref(self) -> None:
        assert not hasattr(graph, "split_graph_ref")

    def test_parse_record_id_unchanged(self) -> None:
        assert parse_record_id("gpu_person:⟨7x⟩") == ("gpu_person", "⟨7x⟩")


class TestToGraphRecord:
    def test_instance(self) -> None:
        assert graph.to_graph_record(GPUPerson(id="a"), "gpu_person", arg="source") == r("a")

    def test_unsaved_instance(self) -> None:
        with pytest.raises(ValueError, match=r"^source must be saved before it can be traversed \(it has no id\)$"):
            graph.to_graph_record(GPUPerson(), "gpu_person", arg="source")

    def test_unsaved_target_names_target(self) -> None:
        with pytest.raises(ValueError, match=r"^target must be saved"):
            graph.to_graph_record(GPUPerson(), "gpu_person", arg="target")

    def test_record_id_returned_as_is(self) -> None:
        rid = RecordId("gpu_person", 1)
        result = graph.to_graph_record(rid, "gpu_person", arg="source")
        assert result is rid
        assert result.id == 1

    def test_table_id_string(self) -> None:
        assert graph.to_graph_record("gpu_city:x", "gpu_person", arg="source") == RecordId("gpu_city", "x")

    def test_backtick_key_unescaped(self) -> None:
        assert graph.to_graph_record("gpu_person:`7x`", "gpu_person", arg="source") == r("7x")

    @pytest.mark.parametrize("value", ["gpu_person:⟨7x⟩", "gpu_person:⟨⟩", "gpu_person:⟨7x", "⟨7x⟩", "gpu_person:7x⟩"])
    def test_angle_bracket_spelling_refused(self, value: str) -> None:
        expected = (
            f"Invalid source: {value!r}. The ⟨…⟩ record-id spelling is not supported; "
            "escape the key with backticks or pass RecordId(table, key)"
        )
        with pytest.raises(ValueError) as excinfo:
            graph.to_graph_record(value, "gpu_person", arg="source")
        assert str(excinfo.value) == expected

    def test_angle_bracket_target_names_target(self) -> None:
        with pytest.raises(ValueError, match=r"^Invalid target: 'gpu_person:⟨7x⟩'\. " + E18):
            graph.to_graph_record("gpu_person:⟨7x⟩", "gpu_person", arg="target")

    def test_numeric_key_stays_a_string(self) -> None:
        result = graph.to_graph_record("gpu_person:1", "gpu_person", arg="source")
        assert result == r("1")
        assert result.id == "1"

    def test_bare_id_uses_default_table(self) -> None:
        assert graph.to_graph_record("x", "gpu_person", arg="source") == r("x")

    @pytest.mark.parametrize("value", ["bad table:x", "gpu_person:", "", "1t:x"])
    def test_bad_string(self, value: str) -> None:
        expected = (
            f"Invalid source: {value!r}. Expected a model instance, a RecordId or a 'table:id' string with an identifier table"
        )
        with pytest.raises(ValueError) as excinfo:
            graph.to_graph_record(value, "gpu_person", arg="source")
        assert str(excinfo.value) == expected

    def test_table_with_trailing_newline(self) -> None:
        with pytest.raises(ValueError, match=r"^Invalid source: "):
            graph.to_graph_record("gpu_person\n:x", "gpu_person", arg="source")

    def test_bad_record_id_table(self) -> None:
        with pytest.raises(ValueError, match=r"^Invalid source: RecordId\(table='bad table'"):
            graph.to_graph_record(RecordId("bad table", "x"), "gpu_person", arg="source")

    def test_empty_record_id_key(self) -> None:
        with pytest.raises(ValueError, match=r"^Invalid source: "):
            graph.to_graph_record(RecordId("gpu_person", ""), "gpu_person", arg="source")

    def test_other_type(self) -> None:
        with pytest.raises(TypeError) as excinfo:
            graph.to_graph_record(5, "gpu_person", arg="source")
        assert str(excinfo.value) == "source must be a model instance, a RecordId or a 'table:id' string, got int"


class TestCheckIncludeSource:
    def test_other_table_with_include_source(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            graph.check_include_source(RecordId("gpu_city", "x"), "gpu_person", True)
        assert str(excinfo.value) == (
            "include_source=True needs a source in 'gpu_person', got 'gpu_city:x': the source would be "
            "returned as a 'gpu_person' record; pass include_source=False"
        )

    def test_other_table_without_include_source(self) -> None:
        graph.check_include_source(RecordId("gpu_city", "x"), "gpu_person", False)

    def test_same_table(self) -> None:
        graph.check_include_source(r("a"), "gpu_person", True)


class TestEdgeTableName:
    def test_string(self) -> None:
        assert graph.edge_table_name("gpu_knows") == "gpu_knows"

    def test_model(self) -> None:
        assert graph.edge_table_name(GPUKnows) == "gpu_knows"

    @pytest.mark.parametrize("value", ["kn ows", "knows\n", "", "1knows", "knows->x"])
    def test_bad_name(self, value: str) -> None:
        expected = (
            f"Invalid edge table: {value!r}. Only letters, digits, and underscores are allowed "
            "(must start with a letter or underscore)."
        )
        with pytest.raises(ValueError) as excinfo:
            graph.edge_table_name(value)
        assert str(excinfo.value) == expected

    @pytest.mark.parametrize("value", [5, GPUKnows(), int])
    def test_other_type(self, value: object) -> None:
        with pytest.raises(TypeError, match=r"^edge must be a table name or a model class, got "):
            graph.edge_table_name(value)

    def test_other_type_text(self) -> None:
        with pytest.raises(TypeError) as excinfo:
            graph.edge_table_name(5)
        assert str(excinfo.value) == "edge must be a table name or a model class, got int"


class TestCheckDirection:
    @pytest.mark.parametrize("direction", ["out", "in", "both"])
    def test_valid(self, direction: str) -> None:
        assert graph.check_direction(direction, allow_both=True, method="reachable_from") == direction

    @pytest.mark.parametrize("direction", ["sideways", "OUT", 5, None])
    def test_bad(self, direction: object) -> None:
        with pytest.raises(ValueError) as excinfo:
            graph.check_direction(direction, allow_both=True, method="reachable_from")
        assert str(excinfo.value) == f"direction must be 'out', 'in' or 'both', got {direction!r}"

    def test_both_refused(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            graph.check_direction("both", allow_both=False, method="paths_from")
        assert str(excinfo.value) == (
            "paths_from() does not support direction='both': each '<->' step also returns the record it "
            "starts from, so every path would repeat its own nodes"
        )


class TestCoerceEdgeValue:
    @pytest.mark.parametrize("value", [GPUPerson(id="b"), RecordId("gpu_person", "b"), "gpu_person:b"])
    def test_out_exact(self, value: object) -> None:
        assert graph.coerce_edge_value("out", "exact", value, frozenset()) == r("b")

    def test_in_list(self) -> None:
        result = graph.coerce_edge_value("in", "in", ["gpu_person:a", RecordId("gpu_person", "c")], frozenset())
        assert result == [r("a"), r("c")]

    def test_not_in_tuple(self) -> None:
        result = graph.coerce_edge_value("out", "not_in", ("gpu_person:a",), frozenset())
        assert result == [r("a")]

    def test_bare_id_refused(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            graph.coerce_edge_value("out", "exact", "b", frozenset())
        assert str(excinfo.value) == (
            "edge_filter on 'out' compares records: pass a model instance, a RecordId or a 'table:id' string, got 'b'"
        )

    def test_bad_list_element_names_it(self) -> None:
        with pytest.raises(ValueError, match=r"^edge_filter on 'in' compares records: .* got 5$"):
            graph.coerce_edge_value("in", "in", ["gpu_person:a", 5], frozenset())

    def test_unsaved_instance(self) -> None:
        with pytest.raises(ValueError, match="unsaved"):
            graph.coerce_edge_value("out", "exact", GPUPerson(), frozenset())

    def test_declared_reference_unchanged(self) -> None:
        assert graph.coerce_edge_value("out", "exact", "$who", frozenset({"who"})) == "$who"

    def test_undeclared_reference_refused(self) -> None:
        with pytest.raises(ValueError, match=r"^edge_filter on 'out' compares records: .* got '\$who'$"):
            graph.coerce_edge_value("out", "exact", "$who", frozenset())

    def test_other_field(self) -> None:
        assert graph.coerce_edge_value("w", "lt", 5, frozenset()) == 5

    def test_other_lookup(self) -> None:
        assert graph.coerce_edge_value("out", "isnull", True, frozenset()) is True


class TestCheckFilter:
    def test_none(self) -> None:
        assert graph.check_filter(None, arg="node_filter") is None

    def test_empty_q(self) -> None:
        assert graph.check_filter(Q(), arg="node_filter") is None

    def test_not_a_q(self) -> None:
        with pytest.raises(TypeError) as excinfo:
            graph.check_filter({"a": 1}, arg="node_filter")
        assert str(excinfo.value) == "node_filter must be a Q object or None, got dict"

    def test_not_a_q_edge(self) -> None:
        with pytest.raises(TypeError, match=r"^edge_filter must be a Q object or None, got str$"):
            graph.check_filter("out = 1", arg="edge_filter", edge=True)

    @pytest.mark.parametrize(
        "q",
        [
            lambda: Q(id__in=Subquery(GPUPerson.objects())),
            lambda: Q(a=1) | Q(id__in=Subquery(GPUPerson.objects())),
            lambda: ~(Q(a=1) & Q(id__in=[Subquery(GPUPerson.objects())])),
        ],
    )
    def test_subquery_refused(self, q: object) -> None:
        with pytest.raises(ValueError) as excinfo:
            graph.check_filter(q(), arg="node_filter")  # type: ignore[operator]
        assert str(excinfo.value) == "Subquery values are not supported in node_filter / edge_filter"

    def test_edge_bare_id_refused(self) -> None:
        with pytest.raises(ValueError, match=r"^edge_filter on 'out' compares records"):
            graph.check_filter(Q(out="b"), arg="edge_filter", edge=True)

    def test_edge_nested_bare_id_refused(self) -> None:
        with pytest.raises(ValueError, match=r"^edge_filter on 'in' compares records"):
            graph.check_filter(Q(w__lt=5) | Q(**{"in": "a"}), arg="edge_filter", edge=True)

    def test_edge_reference_let_through(self) -> None:
        q = Q(out="$who")
        assert graph.check_filter(q, arg="edge_filter", edge=True) is q

    def test_edge_valid_returns_original(self) -> None:
        q = Q(out=GPUPerson(id="b"))
        assert graph.check_filter(q, arg="edge_filter", edge=True) is q
        assert q.children == [("out", "exact", q.children[0][2])]
        assert isinstance(q.children[0][2], GPUPerson)

    def test_node_filter_not_coerced(self) -> None:
        q = Q(out="b")
        assert graph.check_filter(q, arg="node_filter") is q


class TestRenderRange:
    @pytest.mark.parametrize(
        ("lo", "hi", "text"),
        [(1, 3, "1..3"), (1, None, "1.."), (None, 10, "..10"), (None, None, "..")],
    )
    def test_forms(self, lo: int | None, hi: int | None, text: str) -> None:
        assert graph.render_range(lo, hi) == text


class TestRidKey:
    def test_equal(self) -> None:
        assert graph.rid_key(RecordId("t", "a")) == graph.rid_key(RecordId("t", "a"))

    def test_string_and_int_differ(self) -> None:
        assert graph.rid_key(RecordId("t", "1")) != graph.rid_key(RecordId("t", 1))

    def test_table_matters(self) -> None:
        assert graph.rid_key(RecordId("t", "a")) != graph.rid_key(RecordId("u", "a"))

    def test_dict_id(self) -> None:
        assert graph.rid_key(RecordId("t", {"b": 1, "a": 2})) == graph.rid_key(RecordId("t", {"a": 2, "b": 1}))

    def test_hashable(self) -> None:
        assert len({graph.rid_key(RecordId("t", "a")), graph.rid_key(RecordId("t", "a"))}) == 1


class TestAsPath:
    def test_path_ending_at_target(self) -> None:
        path = [r("a"), r("d")]
        assert graph.as_path(path, r("d")) == path

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            [],
            [[RecordId("gpu_person", "e"), RecordId("gpu_person", "d")], [RecordId("gpu_person", "b")]],
            [RecordId("gpu_person", "a"), RecordId("gpu_person", "b")],
            [RecordId("gpu_person", "a"), "x", RecordId("gpu_person", "d")],
            "gpu_person:d",
        ],
    )
    def test_not_a_path(self, raw: object) -> None:
        assert graph.as_path(raw, r("d")) is None


# ---------------------------------------------------------------------------
# U2 — filter renderers bind under a given prefix
# ---------------------------------------------------------------------------


class TestRenderPrefix:
    def test_condition_prefix(self) -> None:
        v: dict[str, Any] = {}
        sql = QuerySet._render_condition("age", "gt", 3, v, [0], prefix="_g")
        assert sql == "age > $_g0"
        assert v == {"_g0": 3}

    def test_q_prefix(self) -> None:
        v: dict[str, Any] = {}
        sql = GPUPerson.objects()._render_q(Q(age__gt=3) | Q(name="x"), v, [0], prefix="_g")
        assert sql == "(age > $_g0 OR name = $_g1)"
        assert v == {"_g0": 3, "_g1": "x"}

    def test_q_prefix_reaches_nested(self) -> None:
        v: dict[str, Any] = {}
        q = Q(age__gt=3) & ~(Q(name="x") | Q(name="y"))
        sql = GPUPerson.objects()._render_q(q, v, [0], prefix="_g")
        assert sql == "(age > $_g0 AND NOT ((name = $_g1 OR name = $_g2)))"
        assert v == {"_g0": 3, "_g1": "x", "_g2": "y"}

    def test_default_coerces_fk(self) -> None:
        v: dict[str, Any] = {}
        GPUWithFK.objects()._render_q(Q(owner="gpu_person:a"), v, [0], prefix="_g")
        assert v == {"_g0": r("a")}

    def test_coerce_override(self) -> None:
        v: dict[str, Any] = {}
        GPUWithFK.objects()._render_q(Q(owner="gpu_person:a"), v, [0], prefix="_g", coerce=lambda f, lk, val, d: val)
        assert v == {"_g0": "gpu_person:a"}

    def test_coerce_spy_called_once_per_leaf(self) -> None:
        calls: list[tuple[str, str, Any, Any]] = []

        def spy(field: str, lookup: str, value: Any, declared: Any) -> Any:
            calls.append((field, lookup, value, declared))
            return value

        qs = GPUPerson.objects().variables(lim=5)
        v: dict[str, Any] = {}
        sql = qs._render_q(
            Q(age__lt="$lim") & (Q(name="x") | Q(name__in=["a", "b"])), v, [0], declared=qs._declared_variables, coerce=spy
        )
        assert sql == "(age < $lim AND (name = $_f0 OR name IN $_f1))"
        assert [c[:3] for c in calls] == [("age", "lt", "$lim"), ("name", "exact", "x"), ("name", "in", ["a", "b"])]
        assert all(set(c[3]) == {"lim"} for c in calls)

    @pytest.mark.parametrize(
        ("lookup", "value", "expected_sql", "expected_vars"),
        [
            ("exact", "x", "name = $_f0", {"_f0": "x"}),
            ("in", ["x", "y"], "name IN $_f0", {"_f0": ["x", "y"]}),
            ("isnull", True, "name IS NULL", {}),
            ("contains", "x", "name CONTAINS $_f0", {"_f0": "x"}),
            ("startswith", "x", "string::starts_with(name, $_f0)", {"_f0": "x"}),
            ("regex", "^x", "string::matches(name, $_f0)", {"_f0": "^x"}),
            ("exact", "$who", "name = $who", {}),
        ],
    )
    def test_default_unchanged(self, lookup: str, value: Any, expected_sql: str, expected_vars: dict[str, Any]) -> None:
        v: dict[str, Any] = {}
        assert QuerySet._render_condition("name", lookup, value, v, [0], None, {"who"}) == expected_sql
        assert v == expected_vars
        v2: dict[str, Any] = {}
        sql = GPUPerson.objects()._render_q(Q(**{f"name__{lookup}": value}), v2, [0], None, {"who"})
        assert sql == expected_sql
        assert v2 == expected_vars

    def test_default_fk_unchanged(self) -> None:
        v: dict[str, Any] = {}
        sql = GPUWithFK.objects()._render_q(Q(owner="gpu_person:a"), v, [0])
        assert sql == "owner = $_f0"
        assert v == {"_f0": r("a")}


# ---------------------------------------------------------------------------
# U10 — exports
# ---------------------------------------------------------------------------


class TestExports:
    def test_exported(self) -> None:
        import src.surreal_orm as orm
        from src.surreal_orm import MAX_GRAPH_DEPTH, MAX_PATH_DEPTH, GraphDirection

        assert MAX_GRAPH_DEPTH == 256
        assert MAX_PATH_DEPTH == 32
        assert GraphDirection is graph.GraphDirection
        for name in ("GraphDirection", "MAX_GRAPH_DEPTH", "MAX_PATH_DEPTH"):
            assert name in orm.__all__

    def test_arrows(self) -> None:
        assert graph.ARROWS == {"out": "->", "in": "<-", "both": "<->"}
