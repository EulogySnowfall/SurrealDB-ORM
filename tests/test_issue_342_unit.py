"""Unit tests for #342 (v2 backport of #327): ``$``-prefixed filter values are bound unless declared.

A filter value starting with ``$`` used to be written into the query text as
is, on the assumption that it named a variable passed through ``.variables()``.
Nothing checked that assumption, so a value such as ``"$x OR true"`` became
part of the WHERE clause instead of a value compared against the field.

A value is now a variable reference only when it is exactly ``$name``
(``re.fullmatch``, so a trailing newline or any other character disqualifies it)
*and* ``name`` was declared with ``.variables()``. Everything else is bound as
``$_fN`` like any other value.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import Field

from src.surreal_orm import BaseSurrealModel, Q, SurrealConfigDict, SurrealDBConnectionManager
from src.surreal_orm.fields import ForeignKey
from src.surreal_orm.model_base import _to_record_link
from src.surreal_orm.subquery import Subquery
from src.surreal_orm.utils import is_declared_variable_reference
from surreal_sdk.protocol.cbor import RecordId

TAUTOLOGY = "$x OR true"
MULTI = "$x; CREATE u342_witness SET v = 2; SELECT * FROM u342_items WHERE true"


class U342Author(BaseSurrealModel):
    """Target of the foreign key."""

    model_config = SurrealConfigDict(table_name="u342_authors")

    id: str | None = None
    name: str = Field(default="")


class U342Item(BaseSurrealModel):
    """A plain string field, an int field and a foreign key."""

    model_config = SurrealConfigDict(table_name="u342_items")

    id: str | None = None
    name: str = Field(default="")
    age: int = 0
    author: ForeignKey("U342Author") = None  # type: ignore[valid-type]


async def _annotate_query(qs: Any) -> tuple[str, dict[str, Any]]:
    """Run ``_execute_annotate()`` against a fake client; return the SQL and variables sent."""
    client = MagicMock()
    client.query = AsyncMock(return_value=MagicMock(all_records=[]))
    with patch.object(SurrealDBConnectionManager, "get_client", new=AsyncMock(return_value=client)):
        await qs._execute_annotate()
    sql, variables = client.query.call_args.args
    return sql, dict(variables)


# ── The predicate ─────────────────────────────────────────────────────────


class TestIsDeclaredVariableReference:
    """The single rule every rendering site applies."""

    @pytest.mark.parametrize("value", ["$a", "$min_age", "$_x1", "$A"])
    def test_declared_identifier_is_a_reference(self, value: str) -> None:
        assert is_declared_variable_reference(value, {value[1:]})

    def test_undeclared_identifier_is_not_a_reference(self) -> None:
        assert not is_declared_variable_reference("$undeclared", {"other"})

    @pytest.mark.parametrize(
        "value",
        [
            "$min_age\n",  # trailing newline: re.match + "$" anchor would accept it
            "$min_age ",
            " $min_age",
            "$min_age.x",
            "$min_age OR true",
            "$min_age;",
            "$1abc",
            "$",
            "min_age",
            "$min-age",
            "$mín_age",
            "$a b",
        ],
    )
    def test_anything_but_an_exact_identifier_is_not_a_reference(self, value: str) -> None:
        assert not is_declared_variable_reference(value, {"min_age", "1abc", "mín_age", "min", "a"})

    @pytest.mark.parametrize("value", [None, 1, ["$a"], b"$a"])
    def test_non_strings_are_not_references(self, value: object) -> None:
        assert not is_declared_variable_reference(value, {"a"})


# ── Keyword filters ───────────────────────────────────────────────────────


class TestKeywordFilters:
    def test_dollar_value_with_operator_text_is_bound(self) -> None:
        qs = U342Item.objects().filter(name=TAUTOLOGY)
        query = qs._compile_query()
        assert query == "SELECT * FROM u342_items WHERE name = $_f0;"
        assert qs._variables == {"_f0": TAUTOLOGY}

    def test_dollar_value_with_statement_separators_is_bound(self) -> None:
        qs = U342Item.objects().filter(name=MULTI)
        query = qs._compile_query()
        assert query == "SELECT * FROM u342_items WHERE name = $_f0;"
        assert qs._variables == {"_f0": MULTI}

    def test_undeclared_reference_is_bound(self) -> None:
        qs = U342Item.objects().filter(name="$undeclared")
        query = qs._compile_query()
        assert "$undeclared" not in query
        assert "name = $_f0" in query
        assert qs._variables == {"_f0": "$undeclared"}

    def test_declared_reference_is_kept(self) -> None:
        qs = U342Item.objects().filter(age__gte="$min_age").variables(min_age=18)
        query = qs._compile_query()
        assert "age >= $min_age" in query
        assert qs._variables == {"min_age": 18}

    def test_declared_before_filter_is_kept(self) -> None:
        qs = U342Item.objects().variables(min_age=18).filter(age__gte="$min_age")
        assert "age >= $min_age" in qs._compile_query()

    @pytest.mark.parametrize("value", ["$min_age\n", "$min_age b"])
    def test_declared_name_followed_by_anything_is_bound(self, value: str) -> None:
        qs = U342Item.objects().filter(age__gte=value).variables(min_age=18)
        query = qs._compile_query()
        assert "age >= $_f0" in query
        assert qs._variables["_f0"] == value

    def test_declared_prefix_followed_by_text_is_bound(self) -> None:
        qs = U342Item.objects().filter(name="$x OR true").variables(x="a")
        query = qs._compile_query()
        assert "name = $_f0" in query
        assert qs._variables["_f0"] == TAUTOLOGY

    def test_function_lookup_binds_undeclared_dollar_value(self) -> None:
        qs = U342Item.objects().filter(name__startswith=TAUTOLOGY)
        query = qs._compile_query()
        assert "string::starts_with(name, $_f0)" in query
        assert qs._variables["_f0"] == TAUTOLOGY

    def test_count_and_where_clause_bind_too(self) -> None:
        """``_compile_where_clause`` backs count(), bulk_update() and bulk_delete()."""
        qs = U342Item.objects().filter(name=TAUTOLOGY)
        assert qs._compile_where_clause() == " WHERE name = $_f0"
        assert qs._variables["_f0"] == TAUTOLOGY


# ── Q objects (and their negation: v2 has no exclude(), ~Q is the form) ──────


class TestQObjects:
    def test_q_binds_undeclared_dollar_value(self) -> None:
        qs = U342Item.objects().filter(Q(name=TAUTOLOGY) | Q(name="bob"))
        query = qs._compile_query()
        assert "(name = $_f0 OR name = $_f1)" in query
        assert qs._variables == {"_f0": TAUTOLOGY, "_f1": "bob"}

    def test_negated_q_binds_undeclared_dollar_value(self) -> None:
        qs = U342Item.objects().filter(~Q(name=TAUTOLOGY))
        query = qs._compile_query()
        assert "NOT (name = $_f0)" in query
        assert qs._variables == {"_f0": TAUTOLOGY}

    def test_q_keeps_declared_reference(self) -> None:
        qs = U342Item.objects().filter(Q(name="$wanted") | Q(name=TAUTOLOGY)).variables(wanted="w")
        query = qs._compile_query()
        assert "(name = $wanted OR name = $_f0)" in query
        assert qs._variables == {"wanted": "w", "_f0": TAUTOLOGY}


# ── Subqueries ────────────────────────────────────────────────────────────


class TestSubqueries:
    def test_inner_undeclared_dollar_value_is_bound(self) -> None:
        inner = U342Item.objects().filter(name=TAUTOLOGY).select("age")
        qs = U342Item.objects().filter(age__in=Subquery(inner))
        query = qs._compile_query()
        assert "(SELECT VALUE age FROM u342_items WHERE name = $_f0)" in query
        assert qs._variables["_f0"] == TAUTOLOGY

    def test_to_surql_binds_too(self) -> None:
        inner = U342Item.objects().filter(name=TAUTOLOGY).select("age")
        variables: dict[str, object] = {}
        sql = Subquery(inner).to_surql(variables, [0])
        assert sql == "(SELECT VALUE age FROM u342_items WHERE name = $_f0)"
        assert variables == {"_f0": TAUTOLOGY}

    def test_inner_declared_reference_is_kept_and_merged(self) -> None:
        inner = U342Item.objects().filter(name="$wanted").variables(wanted="w").select("age")
        qs = U342Item.objects().filter(age__in=Subquery(inner))
        query = qs._compile_query()
        assert "WHERE name = $wanted" in query
        assert qs._variables["wanted"] == "w"

    def test_outer_declared_reference_is_visible_inside(self) -> None:
        inner = U342Item.objects().filter(name="$wanted").select("age")
        qs = U342Item.objects().filter(age__in=Subquery(inner)).variables(wanted="w")
        assert "WHERE name = $wanted" in qs._compile_query()

    async def test_annotate_subquery_binds_undeclared_dollar_value(self) -> None:
        inner = U342Item.objects().filter(name=TAUTOLOGY).select("count()")
        qs = U342Item.objects().values("age").annotate(n=Subquery(inner))
        sql, variables = await _annotate_query(qs)
        assert "WHERE name = $_f0" in sql
        assert variables["_f0"] == TAUTOLOGY

    async def test_annotate_subquery_keeps_declared_reference(self) -> None:
        inner = U342Item.objects().filter(name="$wanted").select("count()")
        qs = U342Item.objects().values("age").annotate(n=Subquery(inner)).variables(wanted="w")
        sql, variables = await _annotate_query(qs)
        assert "WHERE name = $wanted" in sql
        assert variables["wanted"] == "w"


# ── live() ────────────────────────────────────────────────────────────────


class TestLive:
    def test_live_binds_undeclared_dollar_value(self) -> None:
        stream = U342Item.objects().filter(name=TAUTOLOGY).live()
        assert stream._where == "name = $_f0"
        assert stream._params == {"_f0": TAUTOLOGY}

    def test_live_keeps_declared_reference(self) -> None:
        stream = U342Item.objects().filter(name="$wanted").variables(wanted="w").live()
        assert stream._where == "name = $wanted"
        assert stream._params == {"wanted": "w"}


# ── Foreign keys ──────────────────────────────────────────────────────────


class TestForeignKeys:
    def test_undeclared_dollar_value_is_bound_as_a_record_link(self) -> None:
        qs = U342Item.objects().filter(author="$who")
        query = qs._compile_query()
        assert "author = $_f0" in query
        assert qs._variables["_f0"] == RecordId(table="u342_authors", id="$who")

    def test_dollar_value_with_operator_text_is_bound(self) -> None:
        qs = U342Item.objects().filter(author=TAUTOLOGY)
        query = qs._compile_query()
        assert "author = $_f0" in query
        assert qs._variables["_f0"] == RecordId(table="u342_authors", id=TAUTOLOGY)

    def test_declared_reference_is_kept(self) -> None:
        qs = U342Item.objects().filter(author="$who").variables(who="u342_authors:alice")
        assert "author = $who" in qs._compile_query()

    def test_record_link_has_no_dollar_exemption(self) -> None:
        """Write paths always bind; a ``$`` string is a bare ID like any other."""
        assert _to_record_link("$who", "u342_authors") == RecordId(table="u342_authors", id="$who")


# ── Reusing a QuerySet ────────────────────────────────────────────────────


class TestRecompile:
    """Names the ORM binds while compiling are never declared variables.

    Compiling a QuerySet stores its own ``_fN`` bindings in ``_variables``. If
    those counted as declared, a value equal to one of them would bind on the
    first compile and become a reference to another filter's value on the
    second (``count()`` then ``exec()``, pagination, ``exists()`` then ``first()``).
    """

    def test_value_naming_an_orm_binding_is_bound_on_every_compile(self) -> None:
        qs = U342Item.objects().filter(name="acme", age="$_f0")
        for _ in range(2):
            sql = qs._compile_query()
            assert "age = $_f1" in sql
            assert qs._variables["_f0"] == "acme"
            assert qs._variables["_f1"] == "$_f0"

    def test_value_naming_an_orm_binding_is_bound_in_q_on_recompile(self) -> None:
        qs = U342Item.objects().filter(Q(name="acme") & Q(age="$_f0"))
        qs._compile_query()
        sql = qs._compile_query()
        assert "age = $_f1" in sql
        assert qs._variables["_f1"] == "$_f0"

    def test_value_naming_an_orm_binding_is_bound_in_live_after_compile(self) -> None:
        qs = U342Item.objects().filter(name="acme", age="$_f0")
        qs._compile_query()
        stream = qs.live()
        assert stream._where == "name = $_f0 AND age = $_f1"
        assert stream._params is not None
        assert stream._params["_f1"] == "$_f0"

    def test_inner_queryset_compiled_before_wrapping(self) -> None:
        inner = U342Item.objects().filter(name="acme", age="$_f0").select("age")
        inner._compile_query()
        outer = U342Item.objects().filter(age__in=Subquery(inner))
        for _ in range(2):
            sql = outer._compile_query()
            assert "age = $_f1" in sql
            assert outer._variables["_f1"] == "$_f0"

    def test_inner_compiled_bindings_are_not_carried_into_the_outer_query(self) -> None:
        inner = U342Item.objects().filter(name="acme").select("age")
        inner._compile_query()
        assert "_f0" in inner._variables
        outer = U342Item.objects().filter(name="$_f1", age__in=Subquery(inner))
        sql = outer._compile_query()
        assert "name = $_f0" in sql
        assert outer._variables["_f0"] == "$_f1"
        assert outer._variables["_f1"] == "acme"

    async def test_annotate_after_compile(self) -> None:
        inner = U342Item.objects().filter(name="$_f0").select("count()")
        qs = U342Item.objects().filter(name="acme").values("age").annotate(n=Subquery(inner))
        await _annotate_query(qs)
        sql, variables = await _annotate_query(qs)
        assert "WHERE name = $_f0" in sql
        assert "$_f1" in sql
        assert variables["_f1"] == "$_f0"

    def test_declared_variables_survive_recompile(self) -> None:
        qs = U342Item.objects().filter(age__gte="$min_age").variables(min_age=18)
        for _ in range(2):
            assert "age >= $min_age" in qs._compile_query()
            assert qs._variables["min_age"] == 18
