"""Unit tests for #344: ``.variables()`` refuses names starting with ``_``.

Every binding the ORM writes while compiling a query starts with ``_``
(``$_fN``, ``$_sqN``, ``$_knn_vec``, ``$_geo_max``, ``$_g_src``, ``$_up_*``,
...). A declared variable with such a name used to be accepted as a reference
and then silently overwritten by the compiler's own value:
``filter(name="x", age="$_f0").variables(_f0=5)`` compared ``age`` with
``"x"``. The same clash reached a ``Subquery`` whose inner QuerySet declared
the name (``setdefault`` kept whichever value was bound first).
"""

from __future__ import annotations

import pytest

from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.subquery import Subquery


class I344Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i344_items")

    id: str | None = None
    name: str = ""
    age: int = 0


def _message(name: str) -> str:
    return rf"^Variable names starting with '_' are reserved for the ORM's own bindings: '{name}'$"


class TestReservedVariableNames:
    def test_filter_case_raises(self) -> None:
        qs = I344Item.objects().filter(name="x", age="$_f0")
        with pytest.raises(ValueError, match=_message("_f0")):
            qs.variables(_f0=5)

    @pytest.mark.parametrize("name", ["_", "_f0", "_sq0", "_knn_vec", "_geo_max", "_g_src", "_up_x", "_mine", "__dunder"])
    def test_any_name_starting_with_underscore_raises(self, name: str) -> None:
        with pytest.raises(ValueError, match=_message(name)):
            I344Item.objects().variables(**{name: 1})

    def test_subquery_inner_queryset_case_raises(self) -> None:
        inner = I344Item.objects().filter(name="$_sq0").select("age")
        with pytest.raises(ValueError, match=_message("_sq0")):
            inner.variables(_sq0="w")

    def test_rejected_call_leaves_the_queryset_unchanged(self) -> None:
        qs = I344Item.objects().filter(age__gte="$min_age").variables(min_age=18)
        with pytest.raises(ValueError, match=_message("_bad")):
            qs.variables(ok=1, _bad=2)
        assert qs._declared_variables == frozenset({"min_age"})
        assert qs._variables == {"min_age": 18}

    @pytest.mark.parametrize("name", ["t", "min_age", "a_b", "trailing_", "x_f0"])
    def test_names_not_starting_with_underscore_are_accepted(self, name: str) -> None:
        qs = I344Item.objects().filter(age=f"${name}").variables(**{name: 5})
        assert qs._compile_query() == f"SELECT * FROM i344_items WHERE age = ${name};"
        assert qs._variables == {name: 5}

    def test_declared_value_is_no_longer_overwritten(self) -> None:
        # The #344 report, with an accepted name: each condition keeps its value.
        qs = I344Item.objects().filter(name="x", age="$age").variables(age=5)
        assert qs._compile_query() == "SELECT * FROM i344_items WHERE name = $_f0 AND age = $age;"
        assert qs._variables == {"age": 5, "_f0": "x"}

    def test_subquery_with_accepted_inner_name_still_forwards_it(self) -> None:
        inner = I344Item.objects().filter(name="$wanted").variables(wanted="w").select("age")
        qs = I344Item.objects().filter(age__in=Subquery(inner))
        sql = qs._compile_query()
        assert "name = $wanted" in sql
        assert qs._variables["wanted"] == "w"
