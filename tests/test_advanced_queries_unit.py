"""Unit tests for the advanced queries of 0.38.0: ``FieldPath`` and element filters.

``FieldPath`` is a validated builder of SurrealQL idioms over a record's
fields (nested fields, element filters, indexes, destructuring). Every step is
checked at the call, so a path built from user-chosen names can never write
anything but identifiers, bound values and plain integers into a query. These
tests need no server.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import Field

import src.surreal_orm as orm
from src.surreal_orm import BaseSurrealModel, FieldPath, Q, SurrealConfigDict
from src.surreal_orm.constants import ELEMENT_LOOKUPS, VALID_LOOKUPS
from src.surreal_orm.fields import ForeignKey
from src.surreal_orm.paths import _FieldStep, _IndexStep, _PickStep, _WhereStep
from src.surreal_orm.q import check_element_lookup, q_has_condition
from src.surreal_orm.query_set import QuerySet
from src.surreal_orm.subquery import Subquery
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus

HOSTILE_NAMES = ["a; DELETE t", "a`b", "a\n", "a.?.b", "items[0]"]


class AQUOrder(BaseSurrealModel):
    """Orders with an array of line items."""

    model_config = SurrealConfigDict(table_name="aqu_order")

    id: str | None = None
    status: str = Field(default="")
    items: list[dict[str, object]] = Field(default_factory=list)


class AQUCust(BaseSurrealModel):
    """Customers, the target of ``AQUInvoice.buyer``."""

    model_config = SurrealConfigDict(table_name="aqu_cust")

    id: str | None = None
    name: str = ""


class AQUInvoice(BaseSurrealModel):
    """Invoices: a foreign key next to an array of lines."""

    model_config = SurrealConfigDict(table_name="aqu_invoice")

    id: str | None = None
    buyer: ForeignKey("AQUCust") = Field(default=None)  # type: ignore[valid-type]
    lines: list[dict[str, object]] = Field(default_factory=list)


def _subquery() -> Subquery:
    return Subquery(AQUOrder.objects().select("id"))


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_fieldpath_is_exported(self) -> None:
        assert "FieldPath" in orm.__all__
        assert orm.FieldPath is FieldPath


# ---------------------------------------------------------------------------
# q_has_condition()
# ---------------------------------------------------------------------------


class TestQHasCondition:
    @pytest.mark.parametrize(
        "q",
        [Q(), Q() | Q(), Q() & Q(), ~Q(), ~(Q() | Q())],
        ids=["empty", "or-empty", "and-empty", "not-empty", "not-or-empty"],
    )
    def test_without_leaf(self, q: Q) -> None:
        assert q_has_condition(q) is False

    @pytest.mark.parametrize(
        "q",
        [Q(a=1), Q(a=1) | Q(), Q() & Q(a=1), ~Q(a=1), ~(Q() | (Q() & Q(a=1)))],
        ids=["leaf", "or-leaf", "and-leaf", "not-leaf", "deep-leaf"],
    )
    def test_with_leaf(self, q: Q) -> None:
        assert q_has_condition(q) is True


# ---------------------------------------------------------------------------
# U1 — construction, immutability, equality, E1
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_root(self) -> None:
        path = FieldPath("items")
        assert path._root == "items"
        assert path._steps == ()

    def test_dotted_root(self) -> None:
        assert FieldPath("customer.address.city")._root == "customer.address.city"

    def test_each_step_returns_a_new_path(self) -> None:
        root = FieldPath("items")
        cond = Q(qty__gt=1)
        steps = [
            (root.field("sku"), _FieldStep("sku")),
            (root.where(cond), _WhereStep(cond)),
            (root.first(), _IndexStep(0)),
            (root.last(), _IndexStep("$")),
            (root.at(3), _IndexStep(3)),
            (root.pick("sku"), _PickStep((("sku", "sku"),))),
        ]
        for new, step in steps:
            assert new is not root
            assert new._root == "items"
            assert new._steps == (step,)
        assert root._steps == ()

    def test_chained_steps_keep_the_prefix(self) -> None:
        base = FieldPath("items").field("sku")
        longer = base.first()
        assert base._steps == (_FieldStep("sku"),)
        assert longer._steps == (_FieldStep("sku"), _IndexStep(0))

    def test_attributes_are_read_only(self) -> None:
        path = FieldPath("items")
        with pytest.raises(AttributeError):
            path._root = "other"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            path.extra = 1  # type: ignore[attr-defined]

    def test_structural_equality(self) -> None:
        cond = Q(qty__gt=1)
        assert FieldPath("items").where(cond).first() == FieldPath("items").where(cond).first()
        assert FieldPath("items").pick("sku", total="price") == FieldPath("items").pick("sku", total="price")
        assert FieldPath("items") != FieldPath("lines")
        assert FieldPath("items").first() != FieldPath("items").last()
        assert FieldPath("items") != "items"

    def test_q_compared_by_identity(self) -> None:
        assert FieldPath("items").where(Q(qty__gt=1)) != FieldPath("items").where(Q(qty__gt=1))

    def test_unhashable(self) -> None:
        assert FieldPath.__hash__ is None
        with pytest.raises(TypeError):
            hash(FieldPath("items"))

    def test_repr(self) -> None:
        text = repr(FieldPath("items").where(qty__gt=1).field("sku").first().last().at(2).pick("sku", total="price"))
        assert text.startswith("FieldPath('items')")
        for name in (".where(", ".field('sku')", ".first()", ".last()", ".at(2)", ".pick('sku', total='price')"):
            assert name in text

    def test_repr_nested_pick(self) -> None:
        text = repr(FieldPath("orders").pick(lines=FieldPath("lines").first()))
        assert text == "FieldPath('orders').pick(lines=FieldPath('lines').first())"

    @pytest.mark.parametrize("bad", ["a b", "a\n", "a.?.b", "items[0]", "", "a; DELETE t", "a`b", "a..b", ".a", 5])
    def test_e1_root(self, bad: object) -> None:
        expected = (
            f"Invalid FieldPath field: {bad!r}. Expected a field name or a dotted path of field names "
            "(letters, digits and underscores, each starting with a letter or underscore)."
        )
        with pytest.raises(ValueError) as info:
            FieldPath(bad)  # type: ignore[arg-type]
        assert str(info.value) == expected

    @pytest.mark.parametrize("bad", ["a b", "a\n", "a.?.b", "items[0]", "", "a; DELETE t", 5])
    def test_e1_field(self, bad: object) -> None:
        with pytest.raises(ValueError, match=r"^Invalid FieldPath field: "):
            FieldPath("items").field(bad)  # type: ignore[arg-type]

    def test_field_accepts_dotted(self) -> None:
        assert FieldPath("customer").field("address.city")._steps == (_FieldStep("address.city"),)


# ---------------------------------------------------------------------------
# U2 — where(): E2, E3, E4, E17, keys
# ---------------------------------------------------------------------------


def _leaves(q: Q) -> list[tuple[str, str, object]]:
    out: list[tuple[str, str, object]] = []
    for child in q.children:
        if isinstance(child, Q):
            out.extend(_leaves(child))
        else:
            out.append(child)
    return out


class TestWhere:
    def test_kwargs_only(self) -> None:
        step = FieldPath("items").where(qty__gt=1, sku="a")._steps[0]
        assert isinstance(step, _WhereStep)
        assert _leaves(step.condition) == [("qty", "gt", 1), ("sku", "exact", "a")]

    def test_single_positional_kept(self) -> None:
        cond = Q(qty__gt=1)
        step = FieldPath("items").where(cond)._steps[0]
        assert isinstance(step, _WhereStep)
        assert step.condition is cond

    def test_positional_first_then_kwargs_anded(self) -> None:
        first = Q(a=1)
        second = Q(b=2) | Q(c=3)
        step = FieldPath("items").where(first, second, d=4)._steps[0]
        assert isinstance(step, _WhereStep)
        top = step.condition
        assert top.connector == Q.AND and not top.negated
        # ((first & second) & Q(d=4))
        left, right = top.children
        assert isinstance(left, Q) and isinstance(right, Q)
        assert left.children == [first, second]
        assert right.children == [("d", "exact", 4)]

    @pytest.mark.parametrize(
        "args",
        [(), (Q(),), (Q() | Q(),), (Q() & Q(),), (~Q(),), (Q(), Q())],
        ids=["none", "empty", "or-empty", "and-empty", "not-empty", "two-empty"],
    )
    def test_e2(self, args: tuple[Q, ...]) -> None:
        with pytest.raises(ValueError) as info:
            FieldPath("items").where(*args)
        assert str(info.value) == "FieldPath.where() needs at least one condition."

    def test_e2_partial_empty_accepted(self) -> None:
        FieldPath("items").where(Q(a=1) | Q())

    def test_e3(self) -> None:
        with pytest.raises(TypeError) as info:
            FieldPath("items").where({"qty__gt": 1})  # type: ignore[arg-type]
        assert str(info.value) == "FieldPath.where() positional arguments must be Q objects, got 'dict'."

    def test_e4(self) -> None:
        with pytest.raises(TypeError) as info:
            FieldPath("items").where(Q(id__in=_subquery()))
        assert str(info.value) == (
            "A Subquery cannot be used inside an element condition ('id__in'): "
            "element conditions are evaluated per array element."
        )

    def test_e4_kwarg(self) -> None:
        with pytest.raises(TypeError, match=r"\('id__in'\)"):
            FieldPath("items").where(id__in=_subquery())

    def test_e4_nested(self) -> None:
        with pytest.raises(TypeError, match=r"\('id__in'\)"):
            FieldPath("items").where(Q(a=1) | ~Q(id__in=_subquery()))

    def test_e4_in_pick_value(self) -> None:
        with pytest.raises(TypeError, match=r"\('sku__exact'\)"):
            FieldPath("orders").pick(x=FieldPath("y").where(Q(sku=_subquery())))

    def test_unknown_lookup(self) -> None:
        with pytest.raises(ValueError) as info:
            FieldPath("items").where(qty__bogus=1)
        assert "Invalid filter key 'qty__bogus': unknown lookup 'bogus'." in str(info.value)

    def test_bad_key(self) -> None:
        with pytest.raises(ValueError, match=r"^Invalid filter key 'a b'"):
            FieldPath("items").where(**{"a b": 1})

    @pytest.mark.parametrize(
        ("path", "name"),
        [
            (FieldPath("orders").where(status="open").field("lines"), "lines"),
            (FieldPath("orders").where(status="open").pick("a").field("b"), "b"),
            (FieldPath("orders").where(status="open").field("a").field("b"), "b"),
            (FieldPath("orders").where(status="open").field("a.b"), "a.b"),
        ],
        ids=["field", "pick-field", "field-field", "dotted-field"],
    )
    def test_e17_refused(self, path: FieldPath, name: str) -> None:
        with pytest.raises(ValueError) as info:
            path.where(qty__gt=1)
        assert str(info.value) == (
            f"FieldPath.where() cannot follow .field({name!r}) after an earlier where(): it would test each kept "
            f"element's whole {name!r} value instead of filtering inside it. Put the condition in the earlier "
            f"where(), or filter inside each element with pick(<key>=FieldPath({name!r}).where(...))."
        )

    @pytest.mark.parametrize(
        "path",
        [
            FieldPath("orders").field("a"),
            FieldPath("orders").pick("a"),
            FieldPath("orders").where(status="open").pick("a"),
            FieldPath("orders").where(status="open").first().field("a"),
            FieldPath("orders").where(status="open").at(1).pick("x").field("a"),
            FieldPath("orders").where(status="open"),
            FieldPath("a.b"),
            FieldPath("orders").field("a").field("b"),
        ],
        ids=["field", "pick", "where-pick", "where-first-field", "where-at-pick-field", "where", "dotted", "fields"],
    )
    def test_e17_accepted(self, path: FieldPath) -> None:
        assert isinstance(path.where(qty__gt=1)._steps[-1], _WhereStep)

    def test_where_does_not_change_receiver(self) -> None:
        base = FieldPath("items")
        base.where(qty__gt=1)
        assert base._steps == ()


# ---------------------------------------------------------------------------
# U3 — at(): E7
# ---------------------------------------------------------------------------


class TestAt:
    @pytest.mark.parametrize(
        ("bad", "exc", "text"),
        [
            ("1", TypeError, "FieldPath.at() index must be an int, got str"),
            (1.0, TypeError, "FieldPath.at() index must be an int, got float"),
            (True, TypeError, "FieldPath.at() index must be an int, got bool"),
            (-1, ValueError, "FieldPath.at() index must be >= 0, got -1"),
        ],
    )
    def test_e7(self, bad: object, exc: type[Exception], text: str) -> None:
        with pytest.raises(exc) as info:
            FieldPath("items").at(bad)  # type: ignore[arg-type]
        assert str(info.value) == text

    def test_at_zero_equals_first(self) -> None:
        assert FieldPath("items").at(0) == FieldPath("items").first()

    def test_index_is_a_plain_int(self) -> None:
        class Weird(int):
            def __format__(self, spec: str) -> str:
                return "0; DELETE t"

        step = FieldPath("items").at(Weird(2))._steps[0]
        assert isinstance(step, _IndexStep)
        assert type(step.index) is int and step.index == 2


# ---------------------------------------------------------------------------
# U4 — pick(): E8, E9, E10, E11
# ---------------------------------------------------------------------------


class TestPick:
    def test_entries_positional_first_then_aliased(self) -> None:
        sub = FieldPath("lines").first()
        step = FieldPath("items").pick("sku", "qty", total="price", where_="address.city", lines=sub)._steps[0]
        assert isinstance(step, _PickStep)
        assert step.entries == (
            ("sku", "sku"),
            ("qty", "qty"),
            ("total", "price"),
            ("where_", "address.city"),
            ("lines", sub),
        )

    def test_e8(self) -> None:
        with pytest.raises(ValueError) as info:
            FieldPath("items").pick()
        assert str(info.value) == "FieldPath.pick() needs at least one field."

    @pytest.mark.parametrize("bad", ["address.city", "a b", *HOSTILE_NAMES, ""])
    def test_e9_positional(self, bad: str) -> None:
        with pytest.raises(ValueError) as info:
            FieldPath("items").pick(bad)
        assert str(info.value) == (
            f"Invalid FieldPath.pick() field: {bad!r}. Only letters, digits, and underscores are allowed "
            "(must start with a letter or underscore)."
        )

    @pytest.mark.parametrize("bad", ["a b", "a; DELETE t", "a`b", "a\n"])
    def test_e9_key(self, bad: str) -> None:
        with pytest.raises(ValueError, match=r"^Invalid FieldPath\.pick\(\) key: "):
            FieldPath("items").pick(**{bad: "x"})

    @pytest.mark.parametrize("bad", ["a b", *HOSTILE_NAMES, ""])
    def test_e9_source(self, bad: str) -> None:
        with pytest.raises(ValueError, match=r"^Invalid FieldPath\.pick\(\) source: "):
            FieldPath("items").pick(k=bad)

    @pytest.mark.parametrize("bad", [5, None, Q(a=1)])
    def test_e10(self, bad: object) -> None:
        with pytest.raises(TypeError) as info:
            FieldPath("items").pick(k=bad)  # type: ignore[arg-type]
        assert str(info.value) == (
            f"FieldPath.pick() value for 'k' must be a field path string or a FieldPath, got {type(bad).__name__!r}."
        )

    def test_e11_positional_and_alias(self) -> None:
        with pytest.raises(ValueError) as info:
            FieldPath("items").pick("sku", sku="x")
        assert str(info.value) == "FieldPath.pick() key 'sku' is given twice."

    def test_e11_positional_twice(self) -> None:
        with pytest.raises(ValueError, match=r"^FieldPath\.pick\(\) key 'sku' is given twice\.$"):
            FieldPath("items").pick("sku", "sku")


# ---------------------------------------------------------------------------
# iter_where_conditions()
# ---------------------------------------------------------------------------


class TestIterWhereConditions:
    def test_yields_every_condition_including_pick_values(self) -> None:
        a, b, c = Q(a=1), Q(b=2), Q(c=3)
        path = (
            FieldPath("orders")
            .where(a)
            .pick("x", lines=FieldPath("lines").where(b), deep=FieldPath("d").pick(e=FieldPath("e").where(c)))
        )
        assert list(path.iter_where_conditions()) == [a, b, c]

    def test_empty(self) -> None:
        assert list(FieldPath("items").field("x").first().iter_where_conditions()) == []


# ---------------------------------------------------------------------------
# U13 — hostile strings are names nowhere
# ---------------------------------------------------------------------------


class TestHostileNames:
    @pytest.mark.parametrize("bad", HOSTILE_NAMES)
    def test_rejected_everywhere_a_name_is_written(self, bad: str) -> None:
        for build in (
            lambda: FieldPath(bad),
            lambda: FieldPath("items").field(bad),
            lambda: FieldPath("items").pick(bad),
            lambda: FieldPath("items").pick(**{bad: "x"}),
            lambda: FieldPath("items").pick(k=bad),
            lambda: FieldPath("items").where(**{bad: 1}),
        ):
            with pytest.raises(ValueError):
                build()

    @pytest.mark.parametrize("bad", HOSTILE_NAMES)
    def test_kept_as_values(self, bad: str) -> None:
        step = FieldPath("items").where(sku=bad)._steps[0]
        assert isinstance(step, _WhereStep)
        assert step.condition.children == [("sku", "exact", bad)]


# ---------------------------------------------------------------------------
# U6 / U7 / U12 — element filter lookups __any / __all / __none
# ---------------------------------------------------------------------------

E4_ID_IN = (
    "A Subquery cannot be used inside an element condition ('id__in'): element conditions are evaluated per array element."
)


def _e5(lookup: str = "any", field: str = "items") -> str:
    return f"Lookup '{lookup}' on {field!r} needs a non-empty Q() element condition."


def _e6(type_name: str, lookup: str = "any", field: str = "items") -> str:
    return (
        f"Value for lookup '{lookup}' on field '{field}' must be a Q object (the condition each element is tested "
        f"against), got {type_name!r}."
    )


def _orders() -> QuerySet[AQUOrder]:
    return AQUOrder.objects()


class TestElementLookupNames:
    def test_constants(self) -> None:
        assert frozenset({"any", "all", "none"}) == ELEMENT_LOOKUPS
        assert {"any", "all", "none"} <= VALID_LOOKUPS

    def test_field_named_like_a_lookup_is_still_exact(self) -> None:
        qs = _orders().filter(any=1)
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE any = $_f0;"

    def test_relation_queryset_keeps_its_allow_list(self) -> None:
        from src.surreal_orm.fields.relation import RelationInfo
        from src.surreal_orm.relations import RelationQuerySet

        rel_info = RelationInfo(to_model="aqu_order", relation_type="relation", edge_table="aqu_rel")
        rqs = RelationQuerySet(AQUOrder(id="o1"), rel_info, [(rel_info, {})])
        with pytest.raises(ValueError, match=r"lookup 'any' is not supported in relation filters"):
            rqs.filter(items__any=Q(qty__gt=2))


class TestElementLookupSql:
    """C1-C6, C25 (``exec()`` text via ``_compile_query()``) and C11 (``count()``)."""

    def test_c1_any(self) -> None:
        qs = _orders().filter(items__any=Q(qty__gt=2))
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE (type::is_array(items) AND items[WHERE qty > $_f0]);"
        assert qs._variables == {"_f0": 2}

    def test_c2_none(self) -> None:
        qs = _orders().filter(items__none=Q(qty__gt=2))
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE NOT (type::is_array(items) AND items[WHERE qty > $_f0]);"
        assert qs._variables == {"_f0": 2}

    def test_c3_all(self) -> None:
        qs = _orders().filter(items__all=Q(qty__gt=1))
        assert qs._compile_query() == (
            "SELECT * FROM aqu_order WHERE (type::is_array(items) AND NOT (items[WHERE NOT (qty > $_f0)]));"
        )
        assert qs._variables == {"_f0": 1}

    def test_c4_nested(self) -> None:
        qs = _orders().filter(orders__any=Q(status="open", lines__any=Q(qty__gt=2)))
        assert qs._compile_query() == (
            "SELECT * FROM aqu_order WHERE (type::is_array(orders) AND orders[WHERE (status = $_f0 AND "
            "(type::is_array(lines) AND lines[WHERE qty > $_f1]))]);"
        )
        assert qs._variables == {"_f0": "open", "_f1": 2}

    def test_c5_this_startswith(self) -> None:
        qs = _orders().filter(tags__any=Q(this__startswith="x"))
        assert qs._compile_query() == (
            "SELECT * FROM aqu_order WHERE (type::is_array(tags) AND tags[WHERE string::starts_with($this, $_f0)]);"
        )
        assert qs._variables == {"_f0": "x"}

    def test_c6_this_in(self) -> None:
        qs = _orders().filter(tags__any=Q(this__in=["z", "q"]))
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE (type::is_array(tags) AND tags[WHERE $this IN $_f0]);"
        assert qs._variables == {"_f0": ["z", "q"]}

    def test_c25_dotted_field_part(self) -> None:
        qs = _orders().filter(**{"customer.orders__any": Q(total__gt=100)})
        assert qs._compile_query() == (
            "SELECT * FROM aqu_order WHERE (type::is_array(customer.orders) AND customer.orders[WHERE total > $_f0]);"
        )
        assert qs._variables == {"_f0": 100}

    def test_all_with_several_conditions(self) -> None:
        qs = _orders().filter(items__all=Q(qty__gt=1, sku="a"))
        assert qs._compile_query() == (
            "SELECT * FROM aqu_order WHERE (type::is_array(items) AND NOT (items[WHERE NOT ((qty > $_f0 AND sku = $_f1))]));"
        )
        assert qs._variables == {"_f0": 1, "_f1": "a"}

    def test_or_inside_element_condition(self) -> None:
        qs = _orders().filter(Q(items__any=Q(sku="a") | Q(sku="b")) | Q(status="vip"))
        assert qs._compile_query() == (
            "SELECT * FROM aqu_order WHERE ((type::is_array(items) AND items[WHERE (sku = $_f0 OR sku = $_f1)]) "
            "OR status = $_f2);"
        )
        assert qs._variables == {"_f0": "a", "_f1": "b", "_f2": "vip"}

    def test_negated_inside_element_condition(self) -> None:
        qs = _orders().filter(items__any=~Q(sku="a"))
        assert qs._compile_query() == (
            "SELECT * FROM aqu_order WHERE (type::is_array(items) AND items[WHERE NOT (sku = $_f0)]);"
        )

    def test_not_q_any_equals_none(self) -> None:
        negated = _orders().filter(~Q(items__any=Q(qty__gt=2)))
        none = _orders().filter(items__none=Q(qty__gt=2))
        assert negated._compile_query() == none._compile_query()
        assert negated._variables == none._variables

    def test_negations_around_element_leaves(self) -> None:
        def where(q: Q) -> str:
            sql = _orders().filter(q)._compile_query()
            return sql.removeprefix("SELECT * FROM aqu_order WHERE ").removesuffix(";")

        assert where(~Q(items__all=Q(qty=1))) == "NOT (type::is_array(items) AND NOT (items[WHERE NOT (qty = $_f0)]))"
        assert where(~Q(items__none=Q(qty=1))) == "NOT (NOT (type::is_array(items) AND items[WHERE qty = $_f0]))"
        assert where(~~Q(items__any=Q(qty=1))) == "NOT (NOT (type::is_array(items) AND items[WHERE qty = $_f0]))"
        assert where(~Q(items__any=Q(qty=1), b=2)) == (
            "NOT (((type::is_array(items) AND items[WHERE qty = $_f0]) AND b = $_f1))"
        )

    def test_negation_of_other_conditions_unchanged(self) -> None:
        sql = _orders().filter(~(Q(a=1) | Q(b=2)), ~Q(c=3))._compile_query()
        assert sql == "SELECT * FROM aqu_order WHERE NOT ((a = $_f0 OR b = $_f1)) AND NOT (c = $_f2);"

    def test_q_leaf_renders_like_the_keyword(self) -> None:
        via_q = _orders().filter(Q(items__any=Q(qty__gt=2)))
        assert via_q._compile_query() == _orders().filter(items__any=Q(qty__gt=2))._compile_query()

    async def test_c11_count(self) -> None:
        client = AsyncMock()
        client.query = AsyncMock(
            return_value=QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result=[{"count": 2}])])
        )
        with patch("src.surreal_orm.query_set.SurrealDBConnectionManager.get_client", AsyncMock(return_value=client)):
            assert await _orders().filter(items__any=Q(qty__gt=2)).count() == 2
        query, variables = client.query.call_args.args
        assert query == "SELECT count() FROM aqu_order WHERE (type::is_array(items) AND items[WHERE qty > $_f0]) GROUP ALL;"
        assert variables == {"_f0": 2}


class TestElementConditionRules:
    def test_this_is_a_column_outside_element_conditions(self) -> None:
        qs = _orders().filter(this=1)
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE this = $_f0;"

    def test_this_dotted_is_a_field_inside(self) -> None:
        qs = _orders().filter(items__any=Q(**{"this.x": 1}))
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE (type::is_array(items) AND items[WHERE this.x = $_f0]);"

    def test_no_foreign_key_coercion_inside(self) -> None:
        top = AQUInvoice.objects().filter(buyer="aqu_cust:ann")
        top._compile_query()
        assert top._variables == {"_f0": RecordId("aqu_cust", "ann")}

        inner = AQUInvoice.objects().filter(lines__any=Q(buyer="aqu_cust:ann"))
        assert inner._compile_query() == (
            "SELECT * FROM aqu_invoice WHERE (type::is_array(lines) AND lines[WHERE buyer = $_f0]);"
        )
        assert inner._variables == {"_f0": "aqu_cust:ann"}
        assert type(inner._variables["_f0"]) is str

    def test_declared_variable_written_as_reference(self) -> None:
        qs = _orders().variables(min=2).filter(items__any=Q(qty__gt="$min"))
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE (type::is_array(items) AND items[WHERE qty > $min]);"
        assert qs._variables == {"min": 2}

    def test_undeclared_dollar_value_is_bound(self) -> None:
        qs = _orders().filter(items__any=Q(sku="$min"))
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE (type::is_array(items) AND items[WHERE sku = $_f0]);"
        assert qs._variables == {"_f0": "$min"}

    @pytest.mark.parametrize("bad", HOSTILE_NAMES)
    def test_hostile_values_are_bound(self, bad: str) -> None:
        qs = _orders().filter(items__any=Q(sku=bad))
        sql = qs._compile_query()
        assert bad not in sql
        assert qs._variables == {"_f0": bad}

    @pytest.mark.parametrize("bad", HOSTILE_NAMES)
    def test_hostile_keys_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            Q(items__any=Q(**{bad: 1}))
        with pytest.raises(ValueError):
            _orders().filter(**{f"{bad}__any": Q(qty=1)})


class TestCounterContinuity:
    """U7: element conditions bind with the filter's own counter and prefix."""

    def test_keyword_order(self) -> None:
        qs = _orders().filter(a=1, items__any=Q(qty__gt=2), b=3)
        assert qs._compile_query() == (
            "SELECT * FROM aqu_order WHERE a = $_f0 AND (type::is_array(items) AND items[WHERE qty > $_f1]) AND b = $_f2;"
        )
        assert qs._variables == {"_f0": 1, "_f1": 2, "_f2": 3}

    def test_graph_node_filter_prefix(self) -> None:
        qs = _orders().reachable_from(AQUOrder(id="a"), "aqu_rel", max_depth=1, node_filter=Q(items__any=Q(qty__gt=2)))
        sql = qs._compile_query()
        assert "->aqu_rel->aqu_order[WHERE (type::is_array(items) AND items[WHERE qty > $_g0])]" in sql
        assert qs._variables["_g0"] == 2
        assert "_f0" not in qs._variables


class TestElementLookupErrorsAtTheCall:
    """E4 / E5 / E6 raised when the ``Q`` is built or ``filter()`` is called (D15)."""

    @pytest.mark.parametrize("lookup", ["any", "all", "none"])
    @pytest.mark.parametrize(
        "empty",
        [lambda: Q(), lambda: Q() | Q(), lambda: Q() & Q(), lambda: ~Q()],
        ids=["empty", "or-empty", "and-empty", "not-empty"],
    )
    def test_e5_in_q(self, lookup: str, empty: Any) -> None:
        with pytest.raises(ValueError) as excinfo:
            Q(**{f"items__{lookup}": empty()})
        assert str(excinfo.value) == _e5(lookup)

    @pytest.mark.parametrize(
        "empty",
        [lambda: Q(), lambda: Q() | Q(), lambda: Q() & Q(), lambda: ~Q()],
        ids=["empty", "or-empty", "and-empty", "not-empty"],
    )
    def test_e5_in_filter_before_compile(self, empty: Any) -> None:
        with patch.object(QuerySet, "_compile_query") as spy:
            with pytest.raises(ValueError) as excinfo:
                _orders().filter(items__any=empty())
            spy.assert_not_called()
        assert str(excinfo.value) == _e5()

    def test_e5_partial_empty_accepted(self) -> None:
        qs = _orders().filter(items__any=Q(qty=1) | Q())
        assert qs._compile_query() == "SELECT * FROM aqu_order WHERE (type::is_array(items) AND items[WHERE qty = $_f0]);"

    @pytest.mark.parametrize(
        ("value", "type_name"),
        [({"qty__gt": 2}, "dict"), ([1], "list"), ("$x", "str"), (None, "NoneType")],
    )
    def test_e6_in_q(self, value: object, type_name: str) -> None:
        with pytest.raises(TypeError) as excinfo:
            Q(items__all=value)
        assert str(excinfo.value) == _e6(type_name, "all")

    @pytest.mark.parametrize(("value", "type_name"), [({"qty__gt": 2}, "dict"), ([1], "list"), ("$x", "str")])
    def test_e6_in_filter_before_compile(self, value: object, type_name: str) -> None:
        with patch.object(QuerySet, "_compile_query") as spy:
            with pytest.raises(TypeError) as excinfo:
                _orders().variables(x=1).filter(items__none=value)
            spy.assert_not_called()
        assert str(excinfo.value) == _e6(type_name, "none")

    def test_e4_in_q(self) -> None:
        with pytest.raises(TypeError) as excinfo:
            Q(items__any=Q(id__in=_subquery()))
        assert str(excinfo.value) == E4_ID_IN

    def test_e4_nested_in_q(self) -> None:
        with pytest.raises(TypeError) as excinfo:
            Q(items__any=Q(qty=1) | Q(id__in=_subquery()))
        assert str(excinfo.value) == E4_ID_IN

    def test_e4_in_nested_element_condition(self) -> None:
        with pytest.raises(TypeError) as excinfo:
            Q(orders__any=Q(lines__any=Q(id__in=_subquery())))
        assert str(excinfo.value) == E4_ID_IN

    def test_e4_in_filter_before_compile(self) -> None:
        inner = Q(qty=1)
        inner.children.append(("id", "in", _subquery()))
        with patch.object(QuerySet, "_compile_query") as spy:
            with pytest.raises(TypeError) as excinfo:
                _orders().filter(items__any=inner)
            spy.assert_not_called()
        assert str(excinfo.value) == E4_ID_IN

    def test_graph_node_filter(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _orders().reachable_from(AQUOrder(id="a"), "aqu_rel", max_depth=1, node_filter=Q(items__any=Q()))
        assert str(excinfo.value) == _e5()

    def test_check_element_lookup_ignores_other_lookups(self) -> None:
        check_element_lookup("items", "exact", {"a": 1})
        check_element_lookup("items", "contains", Q())


class TestElementLookupErrorsAtCompile:
    """A hand-built ``Q`` child bypasses ``Q.__init__``: the compiler checks again."""

    def _hand_built(self, value: object, lookup: str = "any") -> Q:
        q = Q(a=1)
        q.children = [("items", lookup, value)]
        return q

    def test_e5(self) -> None:
        qs = _orders().filter(self._hand_built(Q()))
        with pytest.raises(ValueError) as excinfo:
            qs._compile_query()
        assert str(excinfo.value) == _e5()

    def test_e6(self) -> None:
        qs = _orders().filter(self._hand_built({"qty": 1}, "all"))
        with pytest.raises(TypeError) as excinfo:
            qs._compile_query()
        assert str(excinfo.value) == _e6("dict", "all")

    def test_e4(self) -> None:
        inner = Q(qty=1)
        inner.children = [("id", "in", _subquery())]
        qs = _orders().filter(self._hand_built(inner))
        with pytest.raises(TypeError) as excinfo:
            qs._compile_query()
        assert str(excinfo.value) == E4_ID_IN

    def test_e4_deeper_hand_built(self) -> None:
        deepest = Q(qty=1)
        deepest.children = [("id", "in", _subquery())]
        middle = Q(qty=1)
        middle.children = [("lines", "any", deepest)]
        qs = _orders().filter(self._hand_built(middle))
        with pytest.raises(TypeError) as excinfo:
            qs._compile_query()
        assert str(excinfo.value) == E4_ID_IN

    def test_e5_when_rendered_condition_is_empty(self) -> None:
        qs = _orders().filter(items__any=Q(qty=1))
        with patch.object(QuerySet, "_render_element_condition", return_value=""):
            with pytest.raises(ValueError) as excinfo:
                qs._compile_query()
        assert str(excinfo.value) == _e5()
