import inspect

import pytest
from pydantic import Field

from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, SurrealDbError
from src.surreal_orm.query_set import QuerySet
from src.surreal_sdk.exceptions import TransactionConflictError, TransactionError


class ModelTest(BaseSurrealModel):
    id: str = Field(...)
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0)


@pytest.fixture(scope="module", autouse=True)
def model_test() -> ModelTest:
    return ModelTest(id="1", name="Test", age=45)


def test_model_get_query_set(model_test: ModelTest) -> None:
    query = model_test.objects()
    assert isinstance(query, QuerySet)


def test_model_get_id(model_test: ModelTest) -> None:
    assert model_test.get_id() == "1"  # cover _data.get("id") is True


def test_queryset_select() -> None:
    qs = ModelTest.objects().select("id", "name")
    assert qs.select_item == ["id", "name"]


def test_queryset_filter() -> None:
    qs = ModelTest.objects().filter(name="Test", age__gt=18)
    assert qs._filters == [("name", "exact", "Test"), ("age", "gt", 18)]
    qs = ModelTest.objects().filter(name__in=["Test", "Test2"], age__gte=18)
    qs = ModelTest.objects().filter(age__lte=45)
    assert qs._filters == [("age", "lte", 45)]
    qs = ModelTest.objects().filter(age__lt=45)
    assert qs._filters == [("age", "lt", 45)]


def test_queryset_variables(model_test: ModelTest) -> None:
    qs = model_test.objects().variables(name="Test")
    assert qs._variables == {"name": "Test"}


def test_queryset_limit(model_test: ModelTest) -> None:
    qs = model_test.objects().limit(100)
    assert qs._limit == 100


def test_queryset_offset(model_test: ModelTest) -> None:
    qs = model_test.objects().offset(100)
    assert qs._offset == 100


def test_queryset_order_by(model_test: ModelTest) -> None:
    qs = model_test.objects().order_by("name")
    assert qs._order_by == "name ASC"


def test_getattr(model_test: ModelTest) -> None:
    assert model_test.name == "Test"
    assert model_test.age == 45
    assert model_test.id == "1"

    with pytest.raises(AttributeError) as exc:
        model_test.no_attribut  # type: ignore

    assert str(exc.value) == "'ModelTest' object has no attribute 'no_attribut'"


def test_str_dunnder(model_test: ModelTest) -> None:
    assert str(model_test) == "id='1' name='Test' age=45"


async def test_failed_model_validation() -> None:
    class ModelTestInvalide(BaseSurrealModel):
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0, le=125)

    with pytest.raises(SurrealDbError) as exc:
        model = ModelTestInvalide(name="Test", age=45)
        await model.save()

    assert str(exc.value) == "Can't create model, the model needs either 'id' field " + "or primary_key in 'model_config'."


def test_class_with_key_specify() -> None:
    class ModelTest3(BaseSurrealModel):
        model_config = SurrealConfigDict(primary_key="email")
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0)
        email: str = Field(..., max_length=100)

    model = ModelTest3(name="Test", age=45, email="test@test.com")  # type: ignore

    assert model.get_id() == "test@test.com"  # type: ignore


# ==================== v0.5.9: New lookup operators ====================


def test_queryset_filter_not_contains() -> None:
    qs = ModelTest.objects().filter(name__not_contains="test")
    assert qs._filters == [("name", "not_contains", "test")]


def test_queryset_filter_containsall() -> None:
    qs = ModelTest.objects().filter(name__containsall=["a", "b"])
    assert qs._filters == [("name", "containsall", ["a", "b"])]


def test_queryset_filter_containsany() -> None:
    qs = ModelTest.objects().filter(name__containsany=["a", "b"])
    assert qs._filters == [("name", "containsany", ["a", "b"])]


def test_queryset_filter_not_in() -> None:
    qs = ModelTest.objects().filter(age__not_in=[1, 2, 3])
    assert qs._filters == [("age", "not_in", [1, 2, 3])]


def test_queryset_compile_not_contains() -> None:
    qs = ModelTest.objects().filter(name__not_contains="test")
    query = qs._compile_query()
    assert "CONTAINSNOT" in query
    assert "name CONTAINSNOT $_f0" in query
    assert qs._variables["_f0"] == "test"


def test_queryset_compile_containsall() -> None:
    qs = ModelTest.objects().filter(name__containsall=["a", "b"])
    query = qs._compile_query()
    assert "CONTAINSALL" in query
    assert "name CONTAINSALL $_f0" in query
    assert qs._variables["_f0"] == ["a", "b"]


def test_queryset_compile_containsany() -> None:
    qs = ModelTest.objects().filter(name__containsany=["a", "b"])
    query = qs._compile_query()
    assert "CONTAINSANY" in query
    assert "name CONTAINSANY $_f0" in query
    assert qs._variables["_f0"] == ["a", "b"]


def test_queryset_compile_not_in() -> None:
    qs = ModelTest.objects().filter(age__not_in=[1, 2])
    query = qs._compile_query()
    assert "NOT IN" in query
    assert "age NOT IN $_f0" in query
    assert qs._variables["_f0"] == [1, 2]


# ==================== SurrealQL operator compilation ====================


def test_queryset_compile_startswith() -> None:
    """startswith generates string::starts_with() function call."""
    qs = ModelTest.objects().filter(name__startswith="Al")
    query = qs._compile_query()
    assert "string::starts_with(name, $_f0)" in query
    assert qs._variables["_f0"] == "Al"


def test_queryset_compile_endswith() -> None:
    """endswith generates string::ends_with() function call."""
    qs = ModelTest.objects().filter(name__endswith="ce")
    query = qs._compile_query()
    assert "string::ends_with(name, $_f0)" in query
    assert qs._variables["_f0"] == "ce"


def test_queryset_compile_like() -> None:
    """like converts LIKE pattern to regex and uses string::matches()."""
    qs = ModelTest.objects().filter(name__like="%ali%")
    query = qs._compile_query()
    assert "string::matches(name, $_f0)" in query
    assert qs._variables["_f0"] == "^.*ali.*$"


def test_queryset_compile_ilike() -> None:
    """ilike converts to case-insensitive regex with (?i) prefix."""
    qs = ModelTest.objects().filter(name__ilike="%ali%")
    query = qs._compile_query()
    assert "string::matches(name, $_f0)" in query
    assert qs._variables["_f0"] == "(?i)^.*ali.*$"


def test_queryset_compile_icontains() -> None:
    """icontains uses string::contains with string::lowercase."""
    qs = ModelTest.objects().filter(name__icontains="HELLO")
    query = qs._compile_query()
    assert "string::contains(string::lowercase(name), $_f0)" in query
    assert qs._variables["_f0"] == "hello"


def test_queryset_compile_regex() -> None:
    """regex uses string::matches() function."""
    qs = ModelTest.objects().filter(name__regex="gr(a|e)y")
    query = qs._compile_query()
    assert "string::matches(name, $_f0)" in query
    assert qs._variables["_f0"] == "gr(a|e)y"


def test_queryset_compile_iregex() -> None:
    """iregex uses string::matches() with (?i) prefix."""
    qs = ModelTest.objects().filter(name__iregex="hello")
    query = qs._compile_query()
    assert "string::matches(name, $_f0)" in query
    assert qs._variables["_f0"] == "(?i)hello"


def test_queryset_compile_match_fts() -> None:
    """match generates @@ operator for full-text search."""
    qs = ModelTest.objects().filter(name__match="quantum")
    query = qs._compile_query()
    assert "name @@ $_f0" in query
    assert qs._variables["_f0"] == "quantum"


def test_queryset_compile_contains() -> None:
    """contains generates CONTAINS operator (arrays and strings)."""
    qs = ModelTest.objects().filter(name__contains="test")
    query = qs._compile_query()
    assert "name CONTAINS $_f0" in query
    assert qs._variables["_f0"] == "test"


def test_like_to_regex_patterns() -> None:
    """like_to_regex correctly converts LIKE patterns to anchored regex."""
    from src.surreal_orm.constants import like_to_regex

    assert like_to_regex("%ali%") == "^.*ali.*$"
    assert like_to_regex("ali%") == "^ali.*$"
    assert like_to_regex("%ali") == "^.*ali$"
    assert like_to_regex("a_i") == "^a.i$"
    assert like_to_regex("exact") == "^exact$"
    assert like_to_regex("%a.b%") == "^.*a\\.b.*$"


# ==================== v0.5.9: TransactionConflictError ====================


def test_transaction_conflict_error_detection() -> None:
    assert TransactionConflictError.is_conflict_error(Exception("Transaction failed: can be retried"))
    assert TransactionConflictError.is_conflict_error(Exception("failed transaction due to conflict"))
    assert TransactionConflictError.is_conflict_error(Exception("document changed by another client"))
    assert not TransactionConflictError.is_conflict_error(Exception("connection timeout"))
    assert not TransactionConflictError.is_conflict_error(Exception("authentication failed"))


def test_transaction_conflict_error_is_transaction_error() -> None:
    assert issubclass(TransactionConflictError, TransactionError)


# ==================== v0.5.9: retry_on_conflict ====================


def test_retry_on_conflict_import() -> None:
    from src.surreal_orm import retry_on_conflict

    assert callable(retry_on_conflict)


def test_retry_on_conflict_decorator() -> None:
    from src.surreal_orm.utils import retry_on_conflict

    @retry_on_conflict(max_retries=3)
    async def dummy() -> str:
        return "ok"

    assert callable(dummy)
    assert dummy.__name__ == "dummy"


# ==================== v0.5.9: relate() / remove_relation() reverse ====================


def test_relate_signature_has_reverse() -> None:
    sig = inspect.signature(ModelTest.relate)
    assert "reverse" in sig.parameters


def test_remove_relation_signature_has_reverse() -> None:
    sig = inspect.signature(ModelTest.remove_relation)
    assert "reverse" in sig.parameters


# ==================== v0.5.9: Copilot review fixes ====================


def test_atomic_ops_reject_invalid_field_name() -> None:
    """Atomic ops must reject field names that could cause SurrealQL injection."""
    import asyncio

    from src.surreal_orm.model_base import _SAFE_IDENTIFIER_RE

    # Valid field names should pass the regex
    assert _SAFE_IDENTIFIER_RE.match("processed_by")
    assert _SAFE_IDENTIFIER_RE.match("tags")
    assert _SAFE_IDENTIFIER_RE.match("_internal")

    # Invalid / injectable field names should be rejected
    assert not _SAFE_IDENTIFIER_RE.match("field; DROP TABLE users")
    assert not _SAFE_IDENTIFIER_RE.match("a-b")
    assert not _SAFE_IDENTIFIER_RE.match("123field")
    assert not _SAFE_IDENTIFIER_RE.match("")

    # The methods themselves should raise ValueError
    with pytest.raises(ValueError, match="Invalid field name"):
        asyncio.run(ModelTest.atomic_append("1", "bad field!", "val"))

    with pytest.raises(ValueError, match="Invalid field name"):
        asyncio.run(ModelTest.atomic_remove("1", "x;DROP", "val"))

    with pytest.raises(ValueError, match="Invalid field name"):
        asyncio.run(ModelTest.atomic_set_add("1", "123bad", "val"))


def test_array_lookup_rejects_string_value() -> None:
    """Array lookup operators must reject non-sequence values like strings."""
    qs = ModelTest.objects().filter(name__containsall="not_a_list")
    with pytest.raises(TypeError, match="must be a list, tuple, or set"):
        qs._compile_query()


def test_array_lookup_rejects_string_value_where_clause() -> None:
    """Same validation in _compile_where_clause."""
    qs = ModelTest.objects().filter(name__in="bad")
    with pytest.raises(TypeError, match="must be a list, tuple, or set"):
        qs._compile_where_clause()


def test_array_lookup_accepts_tuple_and_set() -> None:
    """Array lookups should accept tuples and sets, not just lists."""
    qs = ModelTest.objects().filter(name__in=("a", "b"))
    query = qs._compile_query()
    assert "IN" in query

    qs2 = ModelTest.objects().filter(name__not_in={"x", "y"})
    query2 = qs2._compile_query()
    assert "NOT IN" in query2


def test_retry_on_conflict_ignores_non_surreal_errors() -> None:
    """retry_on_conflict should NOT catch non-SurrealDBError exceptions."""
    import asyncio

    from src.surreal_orm.utils import retry_on_conflict

    call_count = 0

    @retry_on_conflict(max_retries=3)
    async def always_fails() -> None:
        nonlocal call_count
        call_count += 1
        # This contains "conflict" but is NOT a SurrealDBError
        raise RuntimeError("some conflict happened")

    with pytest.raises(RuntimeError, match="some conflict happened"):
        asyncio.run(always_fails())

    # Should NOT have retried — only called once
    assert call_count == 1


# ==================== v0.5.9: Copilot review round 2 fixes ====================


def test_retry_on_conflict_rejects_invalid_params() -> None:
    """retry_on_conflict should reject negative/invalid parameters."""
    from src.surreal_orm.utils import retry_on_conflict

    with pytest.raises(ValueError, match="max_retries must be >= 0"):
        retry_on_conflict(max_retries=-1)

    with pytest.raises(ValueError, match="base_delay must be > 0"):
        retry_on_conflict(base_delay=0)

    with pytest.raises(ValueError, match="base_delay must be > 0"):
        retry_on_conflict(base_delay=-0.5)

    with pytest.raises(ValueError, match="max_delay must be > 0"):
        retry_on_conflict(max_delay=0)

    with pytest.raises(ValueError, match="backoff_factor must be > 0"):
        retry_on_conflict(backoff_factor=-1)


def test_retry_on_conflict_accepts_zero_retries() -> None:
    """max_retries=0 is valid (execute once, no retries)."""
    from src.surreal_orm.utils import retry_on_conflict

    @retry_on_conflict(max_retries=0)
    async def do_nothing() -> str:
        return "ok"

    assert callable(do_nothing)


def test_relation_methods_reject_invalid_names() -> None:
    """relate(), remove_relation(), get_related() must reject invalid relation names."""
    from src.surreal_orm.model_base import _SAFE_IDENTIFIER_RE

    # Valid relation names
    assert _SAFE_IDENTIFIER_RE.match("follows")
    assert _SAFE_IDENTIFIER_RE.match("has_player")
    assert _SAFE_IDENTIFIER_RE.match("_internal_edge")

    # Invalid / injectable relation names
    assert not _SAFE_IDENTIFIER_RE.match("bad;DROP TABLE users")
    assert not _SAFE_IDENTIFIER_RE.match("has-player")
    assert not _SAFE_IDENTIFIER_RE.match("123edge")
    assert not _SAFE_IDENTIFIER_RE.match("")


# ==================== v0.6.0: Q objects ====================


def test_q_basic_or() -> None:
    """Q objects can be combined with | (OR)."""
    from src.surreal_orm.q import Q

    q = Q(name="alice") | Q(name="bob")
    assert q.connector == Q.OR
    assert len(q.children) == 2


def test_q_basic_and() -> None:
    """Q objects can be combined with & (AND)."""
    from src.surreal_orm.q import Q

    q = Q(name="alice") & Q(age__gt=18)
    assert q.connector == Q.AND
    assert len(q.children) == 2


def test_q_negation() -> None:
    """Q objects can be negated with ~."""
    from src.surreal_orm.q import Q

    q = ~Q(status="banned")
    assert q.negated is True
    assert len(q.children) == 1


def test_q_nested_or_and() -> None:
    """Q objects can be nested: (A | B) & C."""
    from src.surreal_orm.q import Q

    q = (Q(name="alice") | Q(name="bob")) & Q(active=True)
    assert q.connector == Q.AND
    assert len(q.children) == 2


def test_q_filter_integration() -> None:
    """Q objects work in filter() and produce correct parameterized SQL."""
    from src.surreal_orm.q import Q

    qs = ModelTest.objects().filter(
        Q(name="alice") | Q(name="bob"),
    )
    query = qs._compile_query()
    assert "OR" in query
    assert "$_f0" in query
    assert "$_f1" in query
    assert qs._variables["_f0"] == "alice"
    assert qs._variables["_f1"] == "bob"


def test_q_mixed_with_kwargs() -> None:
    """Q objects + keyword filters produce AND-joined conditions."""
    from src.surreal_orm.q import Q

    qs = ModelTest.objects().filter(
        Q(name="alice") | Q(name="bob"),
        age__gt=18,
    )
    query = qs._compile_query()
    assert "OR" in query
    assert "AND" in query
    assert "age" in query


def test_q_negation_in_query() -> None:
    """~Q produces NOT(...) in the compiled query."""
    from src.surreal_orm.q import Q

    qs = ModelTest.objects().filter(~Q(name="banned"))
    query = qs._compile_query()
    assert "NOT" in query


# ==================== v0.6.0: filter() validation ====================


def test_filter_rejects_non_q_positional_args() -> None:
    """filter() must raise TypeError for non-Q positional arguments."""
    with pytest.raises(TypeError, match="positional arguments must be Q objects"):
        ModelTest.objects().filter("not_a_q_object")  # type: ignore


def test_filter_isnull_rejects_non_bool() -> None:
    """isnull lookup must raise TypeError for non-bool values."""
    qs = ModelTest.objects().filter(name__isnull="yes")  # type: ignore
    with pytest.raises(TypeError, match="must be a bool"):
        qs._compile_query()


def test_filter_isnull_true() -> None:
    """isnull=True generates IS NULL."""
    qs = ModelTest.objects().filter(name__isnull=True)
    query = qs._compile_query()
    assert "name IS NULL" in query


def test_filter_isnull_false() -> None:
    """isnull=False generates IS NOT NULL."""
    qs = ModelTest.objects().filter(name__isnull=False)
    query = qs._compile_query()
    assert "name IS NOT NULL" in query


# ==================== v0.6.0: SurrealFunc ====================


def test_surreal_func_repr() -> None:
    """SurrealFunc has a useful repr."""
    from src.surreal_orm.surreal_function import SurrealFunc

    sf = SurrealFunc("time::now()")
    assert repr(sf) == "SurrealFunc('time::now()')"


def test_surreal_func_equality() -> None:
    """SurrealFunc equality based on expression."""
    from src.surreal_orm.surreal_function import SurrealFunc

    assert SurrealFunc("time::now()") == SurrealFunc("time::now()")
    assert SurrealFunc("time::now()") != SurrealFunc("rand::uuid()")


def test_build_set_clause_plain_values() -> None:
    """_build_set_clause binds plain values as $_sv_field parameters."""
    clause, variables = ModelTest._build_set_clause({"name": "alice", "age": 25})
    assert "name = $_sv_name" in clause
    assert "age = $_sv_age" in clause
    assert variables["_sv_name"] == "alice"
    assert variables["_sv_age"] == 25


def test_build_set_clause_surreal_func() -> None:
    """_build_set_clause inlines SurrealFunc expressions."""
    from src.surreal_orm.surreal_function import SurrealFunc

    clause, variables = ModelTest._build_set_clause(
        {
            "name": "alice",
            "joined_at": SurrealFunc("time::now()"),
        }
    )
    assert "name = $_sv_name" in clause
    assert "joined_at = time::now()" in clause
    assert "_sv_name" in variables
    assert "_sv_joined_at" not in variables  # SurrealFunc is NOT parameterized


def test_build_set_clause_rejects_invalid_field() -> None:
    """_build_set_clause must reject invalid field names."""
    with pytest.raises(ValueError, match="Invalid field name"):
        ModelTest._build_set_clause({"bad;DROP TABLE": "value"})


# ==================== v0.6.0: remove_all_relations ====================


def test_remove_all_relations_signature() -> None:
    """remove_all_relations exists with correct parameters."""
    sig = inspect.signature(ModelTest.remove_all_relations)
    params = list(sig.parameters.keys())
    assert "relation" in params
    assert "direction" in params
    assert "tx" in params


def test_remove_all_relations_rejects_invalid_name() -> None:
    """remove_all_relations must reject invalid relation names."""
    import asyncio

    model = ModelTest(id="1", name="Test", age=45)
    with pytest.raises(ValueError, match="Invalid relation name"):
        asyncio.run(model.remove_all_relations("bad;DROP TABLE"))


# ==================== v0.6.0: order_by -field ====================


def test_order_by_desc_shorthand() -> None:
    """order_by('-field') sets DESC ordering."""
    qs = ModelTest.objects().order_by("-name")
    assert qs._order_by == "name DESC"


def test_order_by_asc_default() -> None:
    """order_by('field') keeps ASC ordering."""
    qs = ModelTest.objects().order_by("name")
    assert qs._order_by == "name ASC"


# ==================== v0.6.0: Copilot review round 2 fixes ====================


def test_server_values_rejects_invalid_key() -> None:
    """save(server_values=) must reject invalid field names."""
    from src.surreal_orm.surreal_function import SurrealFunc

    model = ModelTest(id="1", name="Test", age=45)
    with pytest.raises(ValueError, match="Invalid server_values key"):
        import asyncio

        asyncio.run(model.save(server_values={"bad;DROP": SurrealFunc("time::now()")}))


def test_server_values_rejects_reserved_field() -> None:
    """save(server_values=) must reject reserved fields like 'id'."""
    from src.surreal_orm.surreal_function import SurrealFunc

    model = ModelTest(id="1", name="Test", age=45)
    with pytest.raises(ValueError, match="server-generated field"):
        import asyncio

        asyncio.run(model.save(server_values={"id": SurrealFunc("rand::uuid()")}))


def test_server_values_rejects_non_surreal_func() -> None:
    """save(server_values=) must reject non-SurrealFunc values."""
    model = ModelTest(id="1", name="Test", age=45)
    with pytest.raises(TypeError, match="must be SurrealFunc instances"):
        import asyncio

        asyncio.run(model.save(server_values={"name": "plain_string"}))  # type: ignore


def test_remove_all_relations_rejects_invalid_direction() -> None:
    """remove_all_relations() must reject invalid direction values."""
    import asyncio

    model = ModelTest(id="1", name="Test", age=45)
    with pytest.raises(ValueError, match="Invalid direction"):
        asyncio.run(model.remove_all_relations("follows", direction="sideways"))  # type: ignore


def test_surreal_function_typo_alias() -> None:
    """SurealFunction (old name) still works as backward-compat alias."""
    from src.surreal_orm.surreal_function import SurealFunction, SurrealFunction

    assert SurealFunction is SurrealFunction


# ── v0.7.0 FR tests ────────────────────────────────────────────────────


class TestFR1MergeRefresh:
    """FR1: merge(refresh=False) — skip extra SELECT after UPDATE."""

    def test_merge_has_refresh_param(self) -> None:
        """merge() must accept a 'refresh' keyword parameter."""
        sig = inspect.signature(BaseSurrealModel.merge)
        assert "refresh" in sig.parameters

    def test_merge_refresh_default_true(self) -> None:
        """refresh defaults to True (backward-compatible)."""
        sig = inspect.signature(BaseSurrealModel.merge)
        assert sig.parameters["refresh"].default is True

    def test_merge_is_coroutine(self) -> None:
        """merge() must be a coroutine function."""
        assert inspect.iscoroutinefunction(BaseSurrealModel.merge)


class TestFR2CallFunction:
    """FR2: call_function() on ConnectionManager and BaseSurrealModel."""

    def test_call_function_on_connection_manager(self) -> None:
        """SurrealDBConnectionManager must have call_function classmethod."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        assert hasattr(SurrealDBConnectionManager, "call_function")
        assert inspect.iscoroutinefunction(SurrealDBConnectionManager.call_function)

    def test_call_function_on_model(self) -> None:
        """BaseSurrealModel must have call_function classmethod."""
        assert hasattr(BaseSurrealModel, "call_function")
        assert inspect.iscoroutinefunction(BaseSurrealModel.call_function)

    def test_call_function_connection_manager_params(self) -> None:
        """call_function() signature: function, params, return_type."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        sig = inspect.signature(SurrealDBConnectionManager.call_function)
        params = list(sig.parameters.keys())
        assert "function" in params
        assert "params" in params
        assert "return_type" in params

    def test_call_function_model_params(self) -> None:
        """BaseSurrealModel.call_function() has same params."""
        sig = inspect.signature(BaseSurrealModel.call_function)
        params = list(sig.parameters.keys())
        assert "function" in params
        assert "params" in params
        assert "return_type" in params


class TestFR3ExtraVars:
    """FR3: extra_vars on save() and merge() for SurrealFunc bound params."""

    def test_save_has_extra_vars_param(self) -> None:
        """save() must accept 'extra_vars' keyword parameter."""
        sig = inspect.signature(BaseSurrealModel.save)
        assert "extra_vars" in sig.parameters

    def test_save_extra_vars_default_none(self) -> None:
        """extra_vars defaults to None."""
        sig = inspect.signature(BaseSurrealModel.save)
        assert sig.parameters["extra_vars"].default is None

    def test_merge_has_extra_vars_param(self) -> None:
        """merge() must accept 'extra_vars' keyword parameter."""
        sig = inspect.signature(BaseSurrealModel.merge)
        assert "extra_vars" in sig.parameters

    def test_merge_extra_vars_default_none(self) -> None:
        """extra_vars defaults to None."""
        sig = inspect.signature(BaseSurrealModel.merge)
        assert sig.parameters["extra_vars"].default is None

    def test_build_set_clause_with_surreal_func(self) -> None:
        """_build_set_clause separates SurrealFunc from regular values."""
        from src.surreal_orm.surreal_function import SurrealFunc

        model = ModelTest(id="1", name="Test", age=45)
        data = {
            "name": "Alice",
            "updated_at": SurrealFunc("time::now()"),
        }
        set_clause, variables = model._build_set_clause(data)
        # SurrealFunc should be inlined, regular value should be parameterized
        assert "time::now()" in set_clause
        assert "name = $_sv_name" in set_clause
        assert variables["_sv_name"] == "Alice"

    def test_execute_save_with_funcs_has_extra_vars(self) -> None:
        """_execute_save_with_funcs must accept extra_vars."""
        sig = inspect.signature(BaseSurrealModel._execute_save_with_funcs)
        assert "extra_vars" in sig.parameters

    def test_extra_vars_collision_rejected_in_save(self) -> None:
        """extra_vars keys that clash with _sv_* bindings raise ValueError."""
        import asyncio

        from src.surreal_orm.surreal_function import SurrealFunc

        model = ModelTest(id="1", name="Test", age=45)
        # _sv_name is generated internally by _build_set_clause for "name"
        with pytest.raises(ValueError, match="conflict with internal bindings"):
            asyncio.run(
                model.save(
                    server_values={"joined_at": SurrealFunc("time::now()")},
                    extra_vars={"_sv_name": "clash"},
                )
            )

    def test_extra_vars_collision_rejected_in_merge(self) -> None:
        """extra_vars keys that clash with _sv_* bindings raise ValueError in merge."""
        import asyncio

        from src.surreal_orm.surreal_function import SurrealFunc

        model = ModelTest(id="1", name="Test", age=45)
        model._db_persisted = True
        # Need a regular value ("name") to create _sv_name binding,
        # plus a SurrealFunc so the raw-query path is taken.
        with pytest.raises(ValueError, match="conflict with internal bindings"):
            asyncio.run(
                model.merge(
                    extra_vars={"_sv_name": "clash"},
                    name="Alice",
                    last_ping=SurrealFunc("time::now()"),
                )
            )

    def test_extra_vars_no_collision_succeeds(self) -> None:
        """extra_vars with non-conflicting keys do not raise."""
        # Just verify _build_set_clause + merge doesn't error
        clause, variables = ModelTest._build_set_clause({"name": "alice"})
        extra = {"password": "secret123"}
        # No overlap → no error
        conflicting = set(variables) & set(extra)
        assert len(conflicting) == 0


class TestFR4FetchClause:
    """FR4: fetch() + FETCH clause in QuerySet."""

    def test_fetch_method_exists(self) -> None:
        """QuerySet must have a fetch() method."""
        assert hasattr(QuerySet, "fetch")

    def test_fetch_stores_fields(self) -> None:
        """fetch() should store field names."""
        qs = ModelTest.objects().fetch("author", "comments")
        assert qs._fetch_fields == ["author", "comments"]

    def test_fetch_returns_self(self) -> None:
        """fetch() should return the QuerySet for chaining."""
        qs = ModelTest.objects()
        result = qs.fetch("author")
        assert result is qs

    def test_compile_query_with_fetch(self) -> None:
        """_compile_query() includes FETCH clause when fetch() is called."""
        qs = ModelTest.objects().fetch("author", "tags")
        query = qs._compile_query()
        assert "FETCH author, tags" in query

    def test_compile_query_fetch_after_limit(self) -> None:
        """FETCH clause comes after LIMIT/START, before semicolon."""
        qs = ModelTest.objects().limit(10).offset(5).fetch("author")
        query = qs._compile_query()
        # FETCH should come after LIMIT and START
        assert query.endswith("FETCH author;")
        limit_pos = query.index("LIMIT")
        fetch_pos = query.index("FETCH")
        assert fetch_pos > limit_pos

    def test_compile_query_no_fetch_by_default(self) -> None:
        """No FETCH clause when fetch() is not called."""
        qs = ModelTest.objects().filter(age__gt=18)
        query = qs._compile_query()
        assert "FETCH" not in query

    def test_select_related_maps_to_fetch(self) -> None:
        """select_related() names should appear in the FETCH clause."""
        qs = ModelTest.objects().select_related("author")
        query = qs._compile_query()
        assert "FETCH author" in query

    def test_fetch_and_select_related_combined(self) -> None:
        """Both fetch() and select_related() fields appear in FETCH clause."""
        qs = ModelTest.objects().fetch("tags").select_related("author")
        query = qs._compile_query()
        assert "FETCH tags, author" in query

    def test_fetch_fields_init_empty(self) -> None:
        """_fetch_fields is empty by default."""
        qs = ModelTest.objects()
        assert qs._fetch_fields == []

    def test_fetch_rejects_invalid_identifier(self) -> None:
        """fetch() must reject names that are not valid SurrealQL identifiers."""
        with pytest.raises(ValueError, match="Invalid FETCH target"):
            ModelTest.objects().fetch("valid", "DROP TABLE users; --")

    def test_fetch_rejects_identifier_starting_with_digit(self) -> None:
        """fetch() must reject names starting with a digit."""
        with pytest.raises(ValueError, match="Invalid FETCH target"):
            ModelTest.objects().fetch("1author")

    def test_select_related_rejects_invalid_identifier(self) -> None:
        """select_related() must reject invalid SurrealQL identifiers."""
        with pytest.raises(ValueError, match="Invalid FETCH target"):
            ModelTest.objects().select_related("ok_field", "bad field!")

    def test_fetch_dedup_preserves_order(self) -> None:
        """Duplicate FETCH targets from fetch() + select_related() are deduped."""
        qs = ModelTest.objects().fetch("author", "tags").select_related("tags", "comments")
        query = qs._compile_query()
        assert "FETCH author, tags, comments" in query


class TestFR5RemoveAllRelationsList:
    """FR5: remove_all_relations() accepts str | list[str]."""

    def test_accepts_single_string(self) -> None:
        """Single string arg passes validation (no ValueError)."""
        model = ModelTest(id="1", name="Test", age=45)
        # Validation should pass — only a DB error (or connection error) may occur
        try:
            import asyncio

            asyncio.get_event_loop().run_until_complete(model.remove_all_relations("has_player"))
        except ValueError:
            pytest.fail("remove_all_relations raised ValueError for valid single string")
        except Exception:
            pass  # Any non-ValueError (connection/auth) is expected

    def test_accepts_list_of_strings(self) -> None:
        """List of strings arg passes validation (no ValueError)."""
        model = ModelTest(id="1", name="Test", age=45)
        try:
            import asyncio

            asyncio.get_event_loop().run_until_complete(model.remove_all_relations(["has_player", "has_action"]))
        except ValueError:
            pytest.fail("remove_all_relations raised ValueError for valid list of strings")
        except Exception:
            pass  # Any non-ValueError (connection/auth) is expected

    def test_rejects_invalid_name_in_list(self) -> None:
        """Invalid relation name in list raises ValueError."""
        import asyncio

        model = ModelTest(id="1", name="Test", age=45)
        with pytest.raises(ValueError, match="Invalid relation name"):
            asyncio.run(model.remove_all_relations(["has_player", "DROP TABLE;--"]))

    def test_rejects_invalid_single_name(self) -> None:
        """Invalid single relation name still raises ValueError."""
        import asyncio

        model = ModelTest(id="1", name="Test", age=45)
        with pytest.raises(ValueError, match="Invalid relation name"):
            asyncio.run(model.remove_all_relations("DROP TABLE;--"))

    def test_type_annotation_accepts_list(self) -> None:
        """The 'relation' param type annotation includes list[str]."""
        sig = inspect.signature(BaseSurrealModel.remove_all_relations)
        param = sig.parameters["relation"]
        annotation = str(param.annotation)
        assert "list" in annotation or "list[str]" in annotation


# =============================================================================
# Feature 2: Generic QuerySet[T] — type-level tests
# =============================================================================


class TestGenericQuerySet:
    """Verify that QuerySet is properly parameterized as Generic[T]."""

    def test_queryset_is_generic(self) -> None:
        """QuerySet should be a Generic class."""
        import typing

        assert hasattr(QuerySet, "__class_getitem__"), "QuerySet must support subscripting (Generic[T])"
        # Subscript should not raise
        alias = QuerySet[ModelTest]
        origin = typing.get_origin(alias)
        assert origin is QuerySet

    def test_objects_returns_typed_queryset(self) -> None:
        """ModelTest.objects() should return a QuerySet parameterised with ModelTest."""
        qs = ModelTest.objects()
        assert isinstance(qs, QuerySet)
        assert qs.model is ModelTest

    def test_queryset_model_preserved_through_chaining(self) -> None:
        """Chained methods should preserve the model reference."""
        qs = ModelTest.objects().filter(age__gte=18).limit(10).offset(5)
        assert qs.model is ModelTest

    def test_queryset_select_preserves_model(self) -> None:
        """select() should preserve model reference."""
        qs = ModelTest.objects().select("id", "name")
        assert qs.model is ModelTest

    def test_queryset_order_by_preserves_model(self) -> None:
        """order_by() should preserve model reference."""
        qs = ModelTest.objects().order_by("-age")
        assert qs.model is ModelTest


# =============================================================================
# Feature 3: get_related() @overload — signature tests
# =============================================================================


class TestGetRelatedOverloads:
    """Verify that get_related() has proper @overload signatures."""

    def test_get_related_has_overloads(self) -> None:
        """get_related should have __overloaded__ or overload metadata."""
        from typing import get_overloads

        overloads = get_overloads(BaseSurrealModel.get_related)
        assert len(overloads) == 2, f"Expected 2 overloads, got {len(overloads)}"

    def test_overload_signatures(self) -> None:
        """First overload should accept model_class: type[_M], second should accept None."""
        from typing import get_overloads

        overloads = get_overloads(BaseSurrealModel.get_related)
        sig0 = inspect.signature(overloads[0])
        sig1 = inspect.signature(overloads[1])

        # First overload: model_class has no None default
        _p0 = sig0.parameters["model_class"]
        # Second overload: model_class defaults to None
        p1 = sig1.parameters["model_class"]
        assert "None" in str(p1.annotation)


# =============================================================================
# Feature 1: Datetime serialization — _restore_datetime_fields + inline fix
# =============================================================================


class TestRestoreDatetimeFields:
    """Verify _restore_datetime_fields re-injects datetime objects."""

    def test_restore_stringified_datetime(self) -> None:
        """If model_dump() stringifies a datetime, _restore_datetime_fields re-injects it."""
        from datetime import UTC, datetime

        class DtModel(BaseSurrealModel):
            id: str | None = None
            created_at: datetime | None = None

        now = datetime(2026, 2, 19, 10, 30, 45, tzinfo=UTC)
        m = DtModel(id="1", created_at=now)

        # Simulate model_dump() returning a stringified datetime
        data: dict[str, object] = {"id": "1", "created_at": "2026-02-19T10:30:45+00:00"}
        result = m._restore_datetime_fields(data)  # type: ignore[arg-type]
        assert isinstance(result["created_at"], datetime)
        assert result["created_at"] == now

    def test_restore_preserves_real_datetime(self) -> None:
        """If model_dump() already has a datetime, it should be left alone."""
        from datetime import UTC, datetime

        class DtModel(BaseSurrealModel):
            id: str | None = None
            created_at: datetime | None = None

        now = datetime(2026, 2, 19, 10, 30, 45, tzinfo=UTC)
        m = DtModel(id="1", created_at=now)

        data: dict[str, object] = {"id": "1", "created_at": now}
        result = m._restore_datetime_fields(data)  # type: ignore[arg-type]
        assert result["created_at"] is now

    def test_restore_ignores_non_datetime_strings(self) -> None:
        """String fields that are NOT datetime should not be touched."""

        class StrModel(BaseSurrealModel):
            id: str | None = None
            name: str = ""

        m = StrModel(id="1", name="hello")
        data: dict[str, object] = {"id": "1", "name": "hello"}
        result = m._restore_datetime_fields(data)  # type: ignore[arg-type]
        assert result["name"] == "hello"

    def test_restore_none_datetime_untouched(self) -> None:
        """None values for datetime fields should not be changed."""
        from datetime import datetime

        class DtModel(BaseSurrealModel):
            id: str | None = None
            created_at: datetime | None = None

        m = DtModel(id="1", created_at=None)
        data: dict[str, object] = {"id": "1", "created_at": None}
        result = m._restore_datetime_fields(data)  # type: ignore[arg-type]
        assert result["created_at"] is None


class TestExtractDatetimeValues:
    """Verify _extract_datetime_values replaces datetimes with markers."""

    def test_extract_single_datetime(self) -> None:
        from datetime import UTC, datetime

        from src.surreal_orm.utils import _extract_datetime_values

        dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        markers: dict[str, str] = {}
        counter = [0]
        result = _extract_datetime_values(dt, markers, counter)

        assert result == "__SURQL_DT_0__"
        assert "__SURQL_DT_0__" in markers
        assert markers["__SURQL_DT_0__"] == f'd"{dt.isoformat()}"'
        assert counter[0] == 1

    def test_extract_from_nested_dict(self) -> None:
        from datetime import UTC, datetime

        from src.surreal_orm.utils import _extract_datetime_values

        dt = datetime(2026, 3, 1, 8, 0, 0, tzinfo=UTC)
        value = {"name": "test", "ts": dt, "nested": {"inner_ts": dt}}
        markers: dict[str, str] = {}
        counter = [0]
        result = _extract_datetime_values(value, markers, counter)

        assert result["name"] == "test"
        assert result["ts"] == "__SURQL_DT_0__"
        assert result["nested"]["inner_ts"] == "__SURQL_DT_1__"
        assert len(markers) == 2
        assert counter[0] == 2

    def test_extract_from_list(self) -> None:
        from datetime import UTC, datetime

        from src.surreal_orm.utils import _extract_datetime_values

        dt = datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC)
        value = [dt, "plain", dt]
        markers: dict[str, str] = {}
        counter = [0]
        result = _extract_datetime_values(value, markers, counter)

        assert result[0] == "__SURQL_DT_0__"
        assert result[1] == "plain"
        assert result[2] == "__SURQL_DT_1__"
        assert counter[0] == 2

    def test_naive_datetime_gets_utc(self) -> None:
        from datetime import datetime

        from src.surreal_orm.utils import _extract_datetime_values

        naive = datetime(2026, 1, 1, 0, 0, 0)
        markers: dict[str, str] = {}
        counter = [0]
        _extract_datetime_values(naive, markers, counter)

        # Should have UTC timezone in the d"..." literal
        assert "+00:00" in markers["__SURQL_DT_0__"]

    def test_no_datetimes_passthrough(self) -> None:
        from src.surreal_orm.utils import _extract_datetime_values

        value = {"key": "value", "num": 42, "items": [1, 2, 3]}
        markers: dict[str, str] = {}
        counter = [0]
        result = _extract_datetime_values(value, markers, counter)

        assert result == value
        assert len(markers) == 0


class TestInlineDictWithDatetime:
    """Verify inline_dict_variables() correctly handles datetime in complex dicts."""

    def test_datetime_in_complex_dict_becomes_d_literal(self) -> None:
        """datetime inside a complex dict should become a d'...' SurrealQL literal."""
        from datetime import UTC, datetime

        from src.surreal_orm.utils import inline_dict_variables

        dt = datetime(2026, 2, 19, 10, 30, 45, tzinfo=UTC)
        query = "UPDATE t SET data = $data;"
        variables = {"data": {"nested": {"ts": dt}}}

        new_query, remaining = inline_dict_variables(query, variables)

        # $data should be replaced (not in remaining)
        assert "data" not in remaining
        # Query should contain d"..." literal, NOT a plain ISO string
        assert 'd"2026-02-19T10:30:45+00:00"' in new_query
        # Should NOT contain the marker placeholder
        assert "__SURQL_DT_" not in new_query

    def test_simple_variables_preserved(self) -> None:
        """Non-complex variables should remain in the bindings dict."""
        from src.surreal_orm.utils import inline_dict_variables

        query = "UPDATE t SET name = $name, data = $data;"
        variables = {"name": "hello", "data": {"nested": {"key": "val"}}}

        new_query, remaining = inline_dict_variables(query, variables)

        assert "name" in remaining
        assert remaining["name"] == "hello"
        assert "data" not in remaining

    def test_no_complex_values_passthrough(self) -> None:
        """If no complex values, query and variables should be unchanged."""
        from src.surreal_orm.utils import inline_dict_variables

        query = "SELECT * FROM t WHERE id = $id;"
        variables = {"id": "abc123"}

        new_query, remaining = inline_dict_variables(query, variables)

        assert new_query == query
        assert remaining == variables


class TestIsDatetimeField:
    """Verify _is_datetime_field detects datetime types correctly."""

    def test_plain_datetime(self) -> None:
        from datetime import datetime

        from src.surreal_orm.model_base import _is_datetime_field

        assert _is_datetime_field(datetime) is True

    def test_optional_datetime(self) -> None:
        from datetime import datetime

        from src.surreal_orm.model_base import _is_datetime_field

        assert _is_datetime_field(datetime | None) is True

    def test_string_type(self) -> None:
        from src.surreal_orm.model_base import _is_datetime_field

        assert _is_datetime_field(str) is False

    def test_optional_string(self) -> None:
        from src.surreal_orm.model_base import _is_datetime_field

        assert _is_datetime_field(str | None) is False

    def test_list_of_datetime(self) -> None:
        from datetime import datetime

        from src.surreal_orm.model_base import _is_datetime_field

        # list[datetime] should NOT match — it's a list, not a datetime
        assert _is_datetime_field(list[datetime]) is False
