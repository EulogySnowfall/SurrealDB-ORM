"""Unit tests for the advanced queries of 0.38.0: ``FieldPath`` and element filters.

``FieldPath`` is a validated builder of SurrealQL idioms over a record's
fields (nested fields, element filters, indexes, destructuring). Every step is
checked at the call, so a path built from user-chosen names can never write
anything but identifiers, bound values and plain integers into a query. These
tests need no server.
"""

from __future__ import annotations

import pytest
from pydantic import Field

import src.surreal_orm as orm
from src.surreal_orm import BaseSurrealModel, FieldPath, Q, SurrealConfigDict
from src.surreal_orm.paths import _FieldStep, _IndexStep, _PickStep, _WhereStep
from src.surreal_orm.q import q_has_condition
from src.surreal_orm.subquery import Subquery

HOSTILE_NAMES = ["a; DELETE t", "a`b", "a\n", "a.?.b", "items[0]"]


class AQUOrder(BaseSurrealModel):
    """Orders with an array of line items."""

    model_config = SurrealConfigDict(table_name="aqu_order")

    id: str | None = None
    status: str = Field(default="")
    items: list[dict[str, object]] = Field(default_factory=list)


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
