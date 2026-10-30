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


# ---------------------------------------------------------------------------
# U3 — reachable_from(): emitted SurrealQL (C1-C5) and bound variables
# ---------------------------------------------------------------------------


def _walk(**kwargs: Any) -> QuerySet[GPUPerson]:
    """``GPUPerson.objects().reachable_from(gpu_person:a, "gpu_knows", ...)``."""
    kwargs.setdefault("max_depth", 3)
    return GPUPerson.objects().reachable_from(GPUPerson(id="a"), "gpu_knows", **kwargs)


def _ok(records: list[dict[str, Any]]) -> Any:
    from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

    return QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=records)])


def _err(message: str) -> Any:
    from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

    return QueryResponse(results=[QueryResult(status=ResponseStatus.ERR, result=message)])


def _patch_client(client: Any) -> Any:
    from unittest.mock import AsyncMock, patch

    return patch(
        "src.surreal_orm.query_set.SurrealDBConnectionManager.get_client",
        new_callable=AsyncMock,
        return_value=client,
    )


def _client(*responses: Any) -> Any:
    from unittest.mock import AsyncMock

    client = AsyncMock()
    client.query = AsyncMock(side_effect=list(responses))
    return client


class TestReachableFromSql:
    def test_returns_queryset(self) -> None:
        qs = GPUPerson.objects()
        assert qs.reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=3) is qs

    def test_c1(self) -> None:
        qs = _walk()
        assert qs._compile_query() == "SELECT * FROM $_g_src.{1..3+collect}(->gpu_knows->gpu_person) WHERE id != $_g_src;"
        assert qs._variables == {"_g_src": r("a")}
        assert isinstance(qs._variables["_g_src"], RecordId)

    def test_c2(self) -> None:
        qs = (
            GPUPerson.objects()
            .reachable_from(GPUPerson(id="a"), GPUKnows, max_depth=3, min_depth=2)
            .filter(active=True)
            .order_by("name")
            .limit(10)
        )
        order = GPUPerson.objects().order_by("name")._order_by
        assert qs._compile_query() == (
            "SELECT * FROM $_g_src.{2..3+collect}(->gpu_knows->gpu_person) "
            f"WHERE active = $_f0 AND id != $_g_src ORDER BY {order} LIMIT 10;"
        )
        assert qs._variables == {"_f0": True, "_g_src": r("a")}

    def test_c3(self) -> None:
        qs = GPUPerson.objects().reachable_from(GPUPerson(id="a"), "gpu_knows", include_source=True)
        assert qs._compile_query() == "SELECT * FROM $_g_src.{1..+collect+inclusive}(->gpu_knows->gpu_person);"
        assert qs._variables == {"_g_src": r("a")}

    def test_c4(self) -> None:
        qs = _walk(edge_filter=Q(w__lt=5), node_filter=Q(active=True))
        assert qs._compile_query() == (
            "SELECT * FROM $_g_src.{1..3+collect}(->gpu_knows[WHERE w < $_g0]->gpu_person[WHERE active = $_g1]) "
            "WHERE id != $_g_src;"
        )
        assert qs._variables == {"_g_src": r("a"), "_g0": 5, "_g1": True}

    @pytest.mark.parametrize(
        ("direction", "step"),
        [("out", "->gpu_knows->gpu_person"), ("in", "<-gpu_knows<-gpu_person"), ("both", "<->gpu_knows<->gpu_person")],
    )
    def test_c5(self, direction: str, step: str) -> None:
        qs = _walk(direction=direction)
        assert qs._compile_query() == f"SELECT * FROM $_g_src.{{1..3+collect}}({step}) WHERE id != $_g_src;"

    def test_no_value_in_text(self) -> None:
        qs = _walk(edge_filter=Q(w__lt=5), node_filter=Q(name="Zed"))
        sql = qs._compile_query()
        assert "gpu_person:a" not in sql and "Zed" not in sql and "5" not in sql.replace("gpu", "")

    def test_source_forms(self) -> None:
        for source in (GPUPerson(id="a"), r("a"), "gpu_person:a", "a"):
            qs = GPUPerson.objects().reachable_from(source, "gpu_knows", max_depth=1)
            qs._compile_query()
            assert qs._variables["_g_src"] == r("a")

    def test_other_table_source(self) -> None:
        qs = GPUPerson.objects().reachable_from("gpu_city:x", "gpu_knows", max_depth=1)
        assert qs._compile_query() == "SELECT * FROM $_g_src.{1..1+collect}(->gpu_knows->gpu_person) WHERE id != $_g_src;"
        assert qs._variables["_g_src"] == RecordId("gpu_city", "x")

    def test_second_call_wins(self) -> None:
        qs = GPUPerson.objects().reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=3)
        qs.reachable_from("gpu_person:b", "gpu_likes", max_depth=2, direction="in")
        assert qs._compile_query() == "SELECT * FROM $_g_src.{1..2+collect}(<-gpu_likes<-gpu_person) WHERE id != $_g_src;"
        assert qs._variables["_g_src"] == r("b")

    def test_select_and_fetch(self) -> None:
        qs = _walk().select("name").fetch("friend")
        assert qs._compile_query() == (
            "SELECT name FROM $_g_src.{1..3+collect}(->gpu_knows->gpu_person) WHERE id != $_g_src FETCH friend;"
        )

    def test_node_filter_fk_coerced(self) -> None:
        qs = GPUWithFK.objects().reachable_from(
            GPUPerson(id="a"), "gpu_knows", max_depth=1, node_filter=Q(owner="gpu_person:b")
        )
        assert qs._compile_query() == (
            "SELECT * FROM $_g_src.{1..1+collect}(->gpu_knows->gpu_with_fk[WHERE owner = $_g0]) WHERE id != $_g_src;"
        )
        assert qs._variables["_g0"] == r("b")
        assert isinstance(qs._variables["_g0"], RecordId)

    @pytest.mark.parametrize(
        "edge_filter",
        [Q(out="gpu_person:b"), Q(out=GPUPerson(id="b")), Q(out=RecordId("gpu_person", "b"))],
    )
    def test_edge_out_coerced(self, edge_filter: Q) -> None:
        qs = _walk(edge_filter=edge_filter)
        assert qs._compile_query() == (
            "SELECT * FROM $_g_src.{1..3+collect}(->gpu_knows[WHERE out = $_g0]->gpu_person) WHERE id != $_g_src;"
        )
        assert qs._variables["_g0"] == r("b")
        assert isinstance(qs._variables["_g0"], RecordId)

    def test_edge_in_coerced(self) -> None:
        qs = _walk(edge_filter=Q(**{"in": RecordId("gpu_person", "a")}))
        assert "[WHERE in = $_g0]" in qs._compile_query()
        assert qs._variables["_g0"] == r("a")

    def test_edge_out_in_list_coerced(self) -> None:
        qs = _walk(edge_filter=Q(out__in=["gpu_person:b", GPUPerson(id="e")]))
        assert "[WHERE out IN $_g0]" in qs._compile_query()
        assert qs._variables["_g0"] == [r("b"), r("e")]
        assert all(isinstance(x, RecordId) for x in qs._variables["_g0"])

    def test_edge_other_field_bound_as_given(self) -> None:
        qs = _walk(edge_filter=Q(w__lt=5))
        qs._compile_query()
        assert qs._variables["_g0"] == 5

    def test_edge_bare_id_refused_at_call(self) -> None:
        with pytest.raises(ValueError, match="edge_filter on 'out' compares records"):
            _walk(edge_filter=Q(out="b"))

    def test_declared_variable_in_step(self) -> None:
        qs = GPUPerson.objects().variables(lim=5)
        qs.reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=3, edge_filter=Q(w__lt="$lim"))
        assert "(->gpu_knows[WHERE w < $lim]->gpu_person)" in qs._compile_query()
        assert qs._variables["lim"] == 5

    def test_declared_endpoint_reference(self) -> None:
        qs = GPUPerson.objects().variables(who=r("b"))
        qs.reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=3, edge_filter=Q(out="$who"))
        assert "(->gpu_knows[WHERE out = $who]->gpu_person)" in qs._compile_query()

    def test_undeclared_endpoint_reference_refused_at_compile(self) -> None:
        qs = _walk(edge_filter=Q(out="$who"))
        with pytest.raises(ValueError, match="edge_filter on 'out' compares records"):
            qs._compile_query()


# ---------------------------------------------------------------------------
# U4 — reachable_from(): argument errors at the call
# ---------------------------------------------------------------------------


class TestReachableFromErrors:
    @pytest.mark.parametrize(
        ("kwargs", "exc", "text"),
        [
            ({"max_depth": "3"}, TypeError, "max_depth must be an int, got str"),
            ({"max_depth": True}, TypeError, "max_depth must be an int, got bool"),
            ({"max_depth": 0}, ValueError, "max_depth must be >= 1, got 0"),
            ({"max_depth": 300}, ValueError, "max_depth must be <= 256 (SurrealDB's recursion limit), got 300"),
            ({"min_depth": 0}, ValueError, "min_depth must be >= 1, got 0"),
            ({"min_depth": "1"}, TypeError, "min_depth must be an int, got str"),
            (
                {"min_depth": 300, "max_depth": None},
                ValueError,
                "min_depth must be <= 256 (SurrealDB's recursion limit), got 300",
            ),
            ({"min_depth": 3, "max_depth": 2}, ValueError, "min_depth (3) must be <= max_depth (2)"),
            ({"direction": "sideways"}, ValueError, "direction must be 'out', 'in' or 'both', got 'sideways'"),
            ({"node_filter": {"a": 1}}, TypeError, "node_filter must be a Q object or None, got dict"),
            ({"edge_filter": {"a": 1}}, TypeError, "edge_filter must be a Q object or None, got dict"),
            (
                {"node_filter": Q(id__in=Subquery(GPUPerson.objects()))},
                ValueError,
                "Subquery values are not supported in node_filter / edge_filter",
            ),
        ],
    )
    def test_kwargs(self, kwargs: dict[str, Any], exc: type[Exception], text: str) -> None:
        kwargs.setdefault("max_depth", 3)
        with pytest.raises(exc) as excinfo:
            GPUPerson.objects().reachable_from(GPUPerson(id="a"), "gpu_knows", **kwargs)
        assert text in str(excinfo.value)

    def test_max_depth_256_accepted(self) -> None:
        assert "{1..256+collect}" in _walk(max_depth=256)._compile_query()

    def test_bad_edge(self) -> None:
        with pytest.raises(ValueError, match=r"Invalid edge table: 'kn ows'\."):
            GPUPerson.objects().reachable_from(GPUPerson(id="a"), "kn ows", max_depth=3)

    def test_edge_type(self) -> None:
        with pytest.raises(TypeError, match="edge must be a table name or a model class, got int"):
            GPUPerson.objects().reachable_from(GPUPerson(id="a"), 5, max_depth=3)  # type: ignore[arg-type]

    def test_source_type(self) -> None:
        with pytest.raises(TypeError, match="source must be a model instance, a RecordId or a 'table:id' string, got int"):
            GPUPerson.objects().reachable_from(5, "gpu_knows", max_depth=3)  # type: ignore[arg-type]

    def test_unsaved_source(self) -> None:
        with pytest.raises(ValueError, match=r"source must be saved before it can be traversed \(it has no id\)"):
            GPUPerson.objects().reachable_from(GPUPerson(), "gpu_knows", max_depth=3)

    def test_bad_source_table(self) -> None:
        with pytest.raises(ValueError, match="Invalid source: 'bad table:x'"):
            GPUPerson.objects().reachable_from("bad table:x", "gpu_knows", max_depth=3)

    def test_angle_source(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            GPUPerson.objects().reachable_from("gpu_person:⟨a⟩", "gpu_knows", max_depth=3)
        assert E18 in str(excinfo.value)

    def test_include_source_other_table(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            GPUPerson.objects().reachable_from("gpu_city:x", "gpu_knows", max_depth=3, include_source=True)
        assert str(excinfo.value) == (
            "include_source=True needs a source in 'gpu_person', got 'gpu_city:x': the source would be returned as "
            "a 'gpu_person' record; pass include_source=False"
        )

    def test_error_leaves_queryset_untouched(self) -> None:
        qs = GPUPerson.objects()
        with pytest.raises(ValueError):
            qs.reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=0)
        assert qs._compile_query() == "SELECT * FROM gpu_person;"


# ---------------------------------------------------------------------------
# U5 — get(id), all() and statement errors with a traversal
# ---------------------------------------------------------------------------


class TestReachableFromExecution:
    async def test_get_runs_on_a_copy(self) -> None:
        import re

        client = _client(_ok([{"id": r("d"), "name": "D"}]), _ok([{"id": r("e"), "name": "E"}]))
        qs = _walk()
        with _patch_client(client):
            first = await qs.get("d")
            second = await qs.get("e")
        assert first.get_id() == "d" and second.get_id() == "e"
        for call, key in zip(client.query.await_args_list, ("d", "e"), strict=True):
            sql, variables = call.args
            assert "FROM $_g_src.{1..3+collect}(->gpu_knows->gpu_person)" in sql
            assert sql.count("id = ") == 1
            match = re.search(r"\bid = \$(\w+)", sql)
            assert match is not None
            assert variables[match.group(1)] == r(key)
            assert isinstance(variables[match.group(1)], RecordId)
        assert qs._filters == []
        assert r("d") not in qs._variables.values() and r("e") not in qs._variables.values()
        client.select.assert_not_awaited()

    async def test_get_with_prior_filter(self) -> None:
        import re

        client = _client(_ok([{"id": r("d"), "name": "D", "active": True}]))
        qs = _walk().filter(active=True)
        with _patch_client(client):
            found = await qs.get("gpu_person:d")
        assert found.get_id() == "d"
        sql, variables = client.query.await_args.args
        assert sql.count("id = ") == 1
        match = re.search(r"\bid = \$(\w+)", sql)
        assert match is not None
        assert variables[match.group(1)] == r("d")
        assert qs._filters == [("active", "exact", True)]

    async def test_get_not_reachable(self) -> None:
        client = _client(_ok([]))
        with _patch_client(client), pytest.raises(GPUPerson.DoesNotExist):
            await _walk().get("z")

    async def test_get_without_traversal_unchanged(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        client = AsyncMock()
        client.select = AsyncMock(return_value=MagicMock(is_empty=False, first={"id": r("d"), "name": "D"}))
        with _patch_client(client):
            found = await GPUPerson.objects().get("d")
        assert found.get_id() == "d"
        client.query.assert_not_awaited()

    async def test_all_honours_traversal(self) -> None:
        client = _client(_ok([{"id": r("b"), "name": "B"}]))
        with _patch_client(client):
            rows = await _walk().all()
        assert [p.get_id() for p in rows] == ["b"]
        sql, _ = client.query.await_args.args
        assert sql == "SELECT * FROM $_g_src.{1..3+collect}(->gpu_knows->gpu_person) WHERE id != $_g_src;"
        client.select.assert_not_awaited()

    async def test_statement_error_raises(self) -> None:
        from surreal_sdk.exceptions import QueryError

        client = _client(_err("Exceeded the idiom recursion limit of 256."))
        with _patch_client(client), pytest.raises(QueryError, match="recursion limit of 256"):
            await _walk().exec()

    async def test_statement_error_without_traversal_unchanged(self) -> None:
        client = _client(_err("Some error"))
        with _patch_client(client):
            assert await GPUPerson.objects().exec() == []


# ---------------------------------------------------------------------------
# AC-2 — counts, aggregations and grouping over a traversal (C6)
# ---------------------------------------------------------------------------


class TestAggregatesOverTraversal:
    async def test_count_c6(self) -> None:
        client = _client(_ok([{"count": 5}]))
        with _patch_client(client):
            assert await _walk().count() == 5
        sql, variables = client.query.await_args.args
        assert sql == "SELECT count() FROM $_g_src.{1..3+collect}(->gpu_knows->gpu_person) WHERE id != $_g_src GROUP ALL;"
        assert variables == {"_g_src": r("a")}

    async def test_count_with_filter(self) -> None:
        client = _client(_ok([{"count": 2}]))
        with _patch_client(client):
            assert await _walk().filter(active=True).count() == 2
        sql, variables = client.query.await_args.args
        assert sql == (
            "SELECT count() FROM $_g_src.{1..3+collect}(->gpu_knows->gpu_person) "
            "WHERE active = $_f0 AND id != $_g_src GROUP ALL;"
        )
        assert variables == {"_f0": True, "_g_src": r("a")}

    async def test_count_include_source_has_no_exclusion(self) -> None:
        client = _client(_ok([{"count": 6}]))
        with _patch_client(client):
            assert await _walk(include_source=True).count() == 6
        sql, _ = client.query.await_args.args
        assert sql == "SELECT count() FROM $_g_src.{1..3+collect+inclusive}(->gpu_knows->gpu_person) GROUP ALL;"

    @pytest.mark.parametrize(
        ("method", "expr", "key", "value"),
        [
            ("sum", "math::sum(w) AS total", "total", 7),
            ("avg", "math::mean(w) AS average", "average", 1.5),
            ("min", "math::min(w) AS minimum", "minimum", 1),
            ("max", "math::max(w) AS maximum", "maximum", 5),
        ],
    )
    async def test_aggregates(self, method: str, expr: str, key: str, value: Any) -> None:
        client = _client(_ok([{key: value}]))
        with _patch_client(client):
            assert await getattr(_walk(), method)("w") == value
        sql, variables = client.query.await_args.args
        assert sql == (f"SELECT {expr} FROM $_g_src.{{1..3+collect}}(->gpu_knows->gpu_person) WHERE id != $_g_src GROUP ALL;")
        assert variables == {"_g_src": r("a")}

    @pytest.mark.parametrize("method", ["count", "sum", "avg", "min", "max"])
    async def test_statement_error_raises(self, method: str) -> None:
        from surreal_sdk.exceptions import QueryError

        client = _client(_err("Exceeded the idiom recursion limit of 256."))
        call = getattr(_walk(), method)
        with _patch_client(client), pytest.raises(QueryError, match="recursion limit of 256"):
            await (call() if method == "count" else call("w"))

    @pytest.mark.parametrize("method", ["count", "sum", "avg", "min", "max"])
    async def test_statement_error_without_traversal_unchanged(self, method: str) -> None:
        client = _client(_err("Some error"))
        call = getattr(GPUPerson.objects(), method)
        with _patch_client(client):
            result = await (call() if method == "count" else call("w"))
        assert result == {"count": 0, "sum": 0}.get(method)

    def test_count_without_traversal_unchanged(self) -> None:
        qs = GPUPerson.objects().filter(active=True)
        assert qs._compile_read_where_clause() == " WHERE active = $_f0"
        assert qs._variables == {"_f0": True}

    def test_values_annotate(self) -> None:
        from src.surreal_orm.aggregations import Count

        qs = _walk().values("active").annotate(n=Count())
        assert qs._compile_annotate_query() == (
            "SELECT active, count() AS n FROM $_g_src.{1..3+collect}(->gpu_knows->gpu_person) "
            "WHERE id != $_g_src GROUP BY active;"
        )
        assert qs._variables == {"_g_src": r("a")}

    async def test_values_annotate_statement_error_raises(self) -> None:
        from src.surreal_orm.aggregations import Count
        from surreal_sdk.exceptions import QueryError

        client = _client(_err("Exceeded the idiom recursion limit of 256."))
        with _patch_client(client), pytest.raises(QueryError, match="recursion limit of 256"):
            await _walk().values("active").annotate(n=Count()).exec()

    def test_write_where_still_free_of_source(self) -> None:
        qs = _walk().filter(active=True)
        qs._compile_read_where_clause()
        assert qs._compile_where_clause() == " WHERE active = $_f0"


# ---------------------------------------------------------------------------
# U6 (write part) — bulk writes refuse a traversal (E14)
# ---------------------------------------------------------------------------


class TestWriteGuards:
    @pytest.mark.parametrize(
        ("method", "call"),
        [
            ("bulk_update", lambda qs: qs.bulk_update({"name": "x"})),
            ("bulk_delete", lambda qs: qs.bulk_delete()),
            ("upsert", lambda qs: qs.upsert({"name": "x"}, id="gpu_person:x")),
            ("bulk_upsert", lambda qs: qs.bulk_upsert([GPUPerson(id="x", name="x")])),
            ("delete_table", lambda qs: qs.delete_table()),
        ],
    )
    async def test_refused_before_io(self, method: str, call: Any) -> None:
        from unittest.mock import AsyncMock, patch

        qs = GPUPerson.objects().reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=2).filter(name="y")
        with (
            patch("src.surreal_orm.query_set.SurrealDBConnectionManager.get_client", new_callable=AsyncMock) as get_client,
            pytest.raises(ValueError) as excinfo,
        ):
            await call(qs)
        assert str(excinfo.value) == f"{method}() cannot be combined with reachable_from()"
        get_client.assert_not_awaited()

    def test_write_where_never_carries_exclusion(self) -> None:
        qs = GPUPerson.objects().reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=2).filter(name="y")
        where = qs._compile_where_clause()
        assert where == " WHERE name = $_f0"
        assert "_g_src" not in where


# ---------------------------------------------------------------------------
# U6 (rest), AC-9 — the other paths refuse a traversal (E14 / E15)
# ---------------------------------------------------------------------------


def _traversal(max_depth: int = 2) -> QuerySet[GPUPerson]:
    return GPUPerson.objects().reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=max_depth)


def _e14(method: str) -> str:
    return f"{method}() cannot be combined with reachable_from()"


class TestReadGuards:
    @pytest.mark.parametrize(
        ("method", "call"),
        [
            ("live", lambda qs: qs.live()),
            ("changes", lambda qs: qs.changes()),
        ],
    )
    def test_streams_refused_at_call(self, method: str, call: Any) -> None:
        from unittest.mock import AsyncMock, patch

        qs = _traversal()
        with (
            patch("src.surreal_orm.query_set.SurrealDBConnectionManager.get_client", new_callable=AsyncMock) as get_client,
            pytest.raises(ValueError) as excinfo,
        ):
            call(qs)
        assert str(excinfo.value) == _e14(method)
        get_client.assert_not_awaited()

    @pytest.mark.parametrize(
        ("method", "call"),
        [
            ("query", lambda qs: qs.query("SELECT * FROM gpu_person")),
            (
                "hybrid_search",
                lambda qs: qs.hybrid_search(vector_field="emb", vector=[0.1], text_field="name", text_query="x"),
            ),
            ("graph_query", lambda qs: qs.graph_query("->gpu_knows->gpu_person")),
        ],
    )
    async def test_async_refused_before_io(self, method: str, call: Any) -> None:
        from unittest.mock import AsyncMock, patch

        qs = _traversal()
        with (
            patch("src.surreal_orm.query_set.SurrealDBConnectionManager.get_client", new_callable=AsyncMock) as get_client,
            pytest.raises(ValueError) as excinfo,
        ):
            await call(qs)
        assert str(excinfo.value) == _e14(method)
        get_client.assert_not_awaited()

    @pytest.mark.parametrize(
        ("method", "call"),
        [
            ("similar_to", lambda qs: qs.similar_to("emb", [0.1], limit=2)),
            ("search", lambda qs: qs.search(name="x")),
            ("nearby", lambda qs: qs.nearby("loc", (0, 0), 10)),
        ],
    )
    def test_ranking_clauses_refused_at_compile(self, method: str, call: Any) -> None:
        qs = call(_traversal())
        with pytest.raises(ValueError) as excinfo:
            qs._compile_query()
        assert str(excinfo.value) == _e14(method)

    @pytest.mark.parametrize(
        ("method", "call"),
        [
            ("similar_to", lambda qs: qs.similar_to("emb", [0.1], limit=2)),
            ("search", lambda qs: qs.search(name="x")),
            ("nearby", lambda qs: qs.nearby("loc", (0, 0), 10)),
        ],
    )
    async def test_ranking_clauses_refused_by_exec_and_count(self, method: str, call: Any) -> None:
        from unittest.mock import AsyncMock, patch

        for terminal in ("exec", "count"):
            qs = call(_traversal())
            with (
                patch("src.surreal_orm.query_set.SurrealDBConnectionManager.get_client", new_callable=AsyncMock) as get_client,
                pytest.raises(ValueError) as excinfo,
            ):
                await getattr(qs, terminal)()
            assert str(excinfo.value) == _e14(method)
            get_client.assert_not_awaited()

    def test_ranking_clauses_without_traversal_unchanged(self) -> None:
        sql = GPUPerson.objects().nearby("loc", (0, 0), 10)._compile_query()
        assert sql.startswith("SELECT * FROM gpu_person WHERE geo::distance(loc")

    def test_subquery_refused(self) -> None:
        outer = GPUPerson.objects().filter(id__in=Subquery(_traversal().select("id")))
        with pytest.raises(ValueError) as excinfo:
            outer._compile_query()
        assert str(excinfo.value) == _e14("Subquery")

    def test_subquery_to_surql_refused(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            Subquery(_traversal()).to_surql({}, [0])
        assert str(excinfo.value) == "Subquery() cannot be combined with reachable_from()"

    def test_prefetch_refused_at_prefetch_related(self) -> None:
        from src.surreal_orm.prefetch import Prefetch

        with pytest.raises(ValueError) as excinfo:
            GPUPerson.objects().prefetch_related(Prefetch("gpu_knows", queryset=_traversal()))
        assert str(excinfo.value) == "Prefetch() cannot be combined with reachable_from()"

    async def test_prefetch_made_traversal_later_refused_before_main_query(self) -> None:
        from unittest.mock import AsyncMock, patch

        from src.surreal_orm.prefetch import Prefetch

        inner = GPUPerson.objects()
        outer = GPUPerson.objects().prefetch_related(Prefetch("gpu_knows", queryset=inner))
        inner.reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=1)
        with (
            patch("src.surreal_orm.query_set.SurrealDBConnectionManager.get_client", new_callable=AsyncMock) as get_client,
            pytest.raises(ValueError) as excinfo,
        ):
            await outer.exec()
        assert str(excinfo.value) == "Prefetch() cannot be combined with reachable_from()"
        get_client.assert_not_awaited()

    def test_prefetch_without_traversal_accepted(self) -> None:
        from src.surreal_orm.prefetch import Prefetch

        qs = GPUPerson.objects().prefetch_related(Prefetch("gpu_knows", queryset=GPUPerson.objects()))
        assert len(qs._prefetch_related) == 1

    async def test_cache_refused(self) -> None:
        from unittest.mock import AsyncMock, patch

        from src.surreal_orm.cache import QueryCache

        saved = (QueryCache._default_ttl, QueryCache._max_size, QueryCache._enabled)
        QueryCache.configure(enabled=True)
        try:
            qs = _traversal().cache(ttl=5)
            with (
                patch("src.surreal_orm.query_set.SurrealDBConnectionManager.get_client", new_callable=AsyncMock) as get_client,
                pytest.raises(ValueError) as excinfo,
            ):
                await qs.exec()
            assert str(excinfo.value) == (
                "cache() cannot be combined with reachable_from(): edge writes do not invalidate cached results"
            )
            get_client.assert_not_awaited()
        finally:
            QueryCache.configure(default_ttl=saved[0], max_size=saved[1], enabled=saved[2])


# ---------------------------------------------------------------------------
# U7, E6, E13 — paths_from() (C7)
# ---------------------------------------------------------------------------

C7 = (
    "LET $_g_paths = $_g_src.{1..3+path+inclusive}(->gpu_knows->gpu_person); "
    "RETURN { paths: $_g_paths, nodes: (SELECT * FROM array::distinct(array::flatten($_g_paths ?? []))) };"
)

E13 = (
    "paths_from() runs on a bare QuerySet; constrain the walk with node_filter= / edge_filter= instead of "
    "filter(), order_by(), limit() and the other read clauses"
)


def _batch(payload: Any) -> Any:
    """The response to a ``LET …; RETURN …;`` batch: the LET's ``NONE``, then *payload*."""
    from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

    return QueryResponse(
        results=[
            QueryResult(status=ResponseStatus.OK, result=None),
            QueryResult(status=ResponseStatus.OK, result=payload),
        ]
    )


def _rec(key: str) -> dict[str, Any]:
    return {"id": r(key), "name": key.upper()}


def _empty_paths() -> Any:
    return _batch({"paths": [], "nodes": []})


async def _paths(source: Any = None, edge: Any = "gpu_knows", qs: Any = None, **kwargs: Any) -> Any:
    kwargs.setdefault("max_depth", 3)
    queryset = GPUPerson.objects() if qs is None else qs
    return await queryset.paths_from(GPUPerson(id="a") if source is None else source, edge, **kwargs)


class TestPathsFromSql:
    async def test_c7(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            assert await _paths() == []
        sql, variables = client.query.await_args.args
        assert sql == C7
        assert variables == {"_g_src": r("a")}
        assert isinstance(variables["_g_src"], RecordId)

    async def test_include_source_false(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            await _paths(include_source=False)
        sql, _ = client.query.await_args.args
        assert sql == C7.replace("+path+inclusive", "+path")

    async def test_min_depth(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            await _paths(min_depth=2)
        sql, _ = client.query.await_args.args
        assert "$_g_src.{2..3+path+inclusive}(" in sql

    async def test_direction_in(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            await _paths(direction="in")
        sql, _ = client.query.await_args.args
        assert "(<-gpu_knows<-gpu_person)" in sql

    async def test_edge_model(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            await _paths(edge=GPUKnows)
        sql, _ = client.query.await_args.args
        assert sql == C7

    async def test_step_filters_as_c4(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            await _paths(edge_filter=Q(w__lt=5), node_filter=Q(active=True))
        sql, variables = client.query.await_args.args
        assert "$_g_src.{1..3+path+inclusive}(->gpu_knows[WHERE w < $_g0]->gpu_person[WHERE active = $_g1])" in sql
        assert variables == {"_g_src": r("a"), "_g0": 5, "_g1": True}

    async def test_edge_filter_coerces_record(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            await _paths(edge_filter=Q(out=GPUPerson(id="b")))
        sql, variables = client.query.await_args.args
        assert "(->gpu_knows[WHERE out = $_g0]->gpu_person)" in sql
        assert variables["_g0"] == r("b")
        assert isinstance(variables["_g0"], RecordId)

    async def test_declared_variables_accepted_and_sent(self) -> None:
        client = _client(_empty_paths())
        qs = GPUPerson.objects().variables(x=1)
        with _patch_client(client):
            await _paths(qs=qs, node_filter=Q(active="$x"))
        sql, variables = client.query.await_args.args
        assert "->gpu_person[WHERE active = $x])" in sql
        assert variables == {"x": 1, "_g_src": r("a")}
        assert qs._variables == {"x": 1}

    async def test_depth_32_accepted(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            await _paths(max_depth=32)
        sql, _ = client.query.await_args.args
        assert "{1..32+path+inclusive}" in sql

    async def test_other_table_without_include_source(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            await _paths(source="gpu_city:x", edge="gpu_knows", max_depth=2, include_source=False)
        sql, variables = client.query.await_args.args
        assert "$_g_src.{1..2+path}(->gpu_knows->gpu_person)" in sql
        assert variables["_g_src"] == RecordId("gpu_city", "x")


class TestPathsFromMapping:
    async def test_order_kept_and_typed(self) -> None:
        payload = {"paths": [[r("a"), r("b"), r("a")], [r("a"), r("e")]], "nodes": [_rec("a"), _rec("b"), _rec("e")]}
        client = _client(_batch(payload))
        with _patch_client(client):
            paths = await _paths()
        assert [[p.get_id() for p in path] for path in paths] == [["a", "b", "a"], ["a", "e"]]
        assert all(isinstance(p, GPUPerson) for path in paths for p in path)
        assert [p.name for p in paths[0]] == ["A", "B", "A"]
        assert paths[0][0] is paths[0][2] is paths[1][0]

    async def test_missing_node_dropped_with_one_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        payload = {
            "paths": [[r("a"), r("x")], [r("a"), r("b")], [r("a"), r("y"), r("x")]],
            "nodes": [_rec("a"), _rec("b")],
        }
        client = _client(_batch(payload))
        with _patch_client(client), caplog.at_level(logging.WARNING):
            paths = await _paths()
        assert [[p.get_id() for p in path] for path in paths] == [["a", "b"]]
        warnings = [rec for rec in caplog.records if rec.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "2" in warnings[0].getMessage()

    async def test_no_warning_when_complete(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        payload = {"paths": [[r("a"), r("b")]], "nodes": [_rec("a"), _rec("b")]}
        client = _client(_batch(payload))
        with _patch_client(client), caplog.at_level(logging.WARNING):
            await _paths()
        assert [rec for rec in caplog.records if rec.levelno >= logging.WARNING] == []

    async def test_empty(self) -> None:
        client = _client(_empty_paths())
        with _patch_client(client):
            assert await _paths() == []

    async def test_statement_error(self) -> None:
        from surreal_sdk.exceptions import QueryError
        from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

        response = QueryResponse(
            results=[
                QueryResult(status=ResponseStatus.ERR, result="Something failed"),
                QueryResult(status=ResponseStatus.ERR, result="The query was not executed due to a failed transaction"),
            ]
        )
        client = _client(response)
        with _patch_client(client), pytest.raises(QueryError, match="Something failed"):
            await _paths()

    async def test_query_logger(self) -> None:
        from src.surreal_orm.debug import QueryLogger

        client = _client(_empty_paths())
        with _patch_client(client):
            async with QueryLogger() as log:
                await _paths()
        assert [q.sql for q in log.queries] == [C7]
        assert log.queries[0].variables == {"_g_src": r("a")}


class TestPathsFromErrors:
    async def _refused(self, exc: type[Exception], message: str, qs: Any = None, **kwargs: Any) -> None:
        from unittest.mock import AsyncMock, patch

        with (
            patch("src.surreal_orm.query_set.SurrealDBConnectionManager.get_client", new_callable=AsyncMock) as get_client,
            pytest.raises(exc) as excinfo,
        ):
            await _paths(qs=qs, **kwargs)
        assert str(excinfo.value) == message
        get_client.assert_not_awaited()

    async def test_direction_both(self) -> None:
        await self._refused(
            ValueError,
            "paths_from() does not support direction='both': each '<->' step also returns the record it "
            "starts from, so every path would repeat its own nodes",
            direction="both",
        )

    async def test_max_depth_none(self) -> None:
        await self._refused(TypeError, "max_depth must be an int, got NoneType", max_depth=None)

    async def test_max_depth_missing(self) -> None:
        with pytest.raises(TypeError):
            await GPUPerson.objects().paths_from(GPUPerson(id="a"), "gpu_knows")  # type: ignore[call-arg]

    async def test_max_depth_over_cap(self) -> None:
        await self._refused(
            ValueError,
            "max_depth must be <= 32 for paths_from() (the number of paths grows exponentially with the depth), got 33",
            max_depth=33,
        )

    async def test_min_depth_over_cap(self) -> None:
        await self._refused(
            ValueError,
            "min_depth must be <= 32 for paths_from() (the number of paths grows exponentially with the depth), got 33",
            max_depth=32,
            min_depth=33,
        )

    async def test_include_source_other_table(self) -> None:
        await self._refused(
            ValueError,
            "include_source=True needs a source in 'gpu_person', got 'gpu_city:x': the source would be returned "
            "as a 'gpu_person' record; pass include_source=False",
            source="gpu_city:x",
            max_depth=2,
        )

    async def test_bad_edge_filter(self) -> None:
        await self._refused(TypeError, "edge_filter must be a Q object or None, got dict", edge_filter={"w": 1})

    @pytest.mark.parametrize(
        "clause",
        [
            pytest.param(lambda qs: qs.filter(active=True), id="filter"),
            pytest.param(lambda qs: qs.filter(Q(active=True)), id="Q"),
            pytest.param(lambda qs: qs.order_by("name"), id="order_by"),
            pytest.param(lambda qs: qs.limit(1), id="limit"),
            pytest.param(lambda qs: qs.offset(1), id="offset"),
            pytest.param(lambda qs: qs.select("name"), id="select"),
            pytest.param(lambda qs: qs.values("active"), id="values"),
            pytest.param(lambda qs: qs.annotate(n=_count()), id="annotate"),
            pytest.param(lambda qs: qs.fetch("owner"), id="fetch"),
            pytest.param(lambda qs: qs.select_related("owner"), id="select_related"),
            pytest.param(lambda qs: qs.prefetch_related("gpu_knows"), id="prefetch_related"),
            pytest.param(lambda qs: qs.cache(ttl=5), id="cache"),
            pytest.param(lambda qs: qs.similar_to("emb", [0.1], limit=2), id="similar_to"),
            pytest.param(lambda qs: qs.search(name="x"), id="search"),
            pytest.param(lambda qs: qs.nearby("loc", (0, 0), 10), id="nearby"),
            pytest.param(lambda qs: qs.reachable_from(GPUPerson(id="a"), "gpu_knows", max_depth=1), id="reachable_from"),
            pytest.param(lambda qs: qs.traverse("->gpu_knows->gpu_person"), id="traverse"),
        ],
    )
    async def test_read_clause_refused(self, clause: Any) -> None:
        await self._refused(ValueError, E13, qs=clause(GPUPerson.objects()))


def _count() -> Any:
    from src.surreal_orm.aggregations import Count

    return Count()
