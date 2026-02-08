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


async def failed_model_validation() -> None:
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


class TestFR5RemoveAllRelationsList:
    """FR5: remove_all_relations() accepts str | list[str]."""

    def test_accepts_single_string(self) -> None:
        """Single string still works (backward compatible)."""
        import asyncio

        model = ModelTest(id="1", name="Test", age=45)
        # Will fail at DB call, but should not fail at validation
        with pytest.raises(Exception, match="(?:Connection|connect)"):
            asyncio.run(model.remove_all_relations("has_player"))

    def test_accepts_list_of_strings(self) -> None:
        """List of strings is accepted."""
        import asyncio

        model = ModelTest(id="1", name="Test", age=45)
        with pytest.raises(Exception, match="(?:Connection|connect)"):
            asyncio.run(model.remove_all_relations(["has_player", "has_action"]))

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
