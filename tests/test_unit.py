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
    assert "name CONTAINSNOT 'test'" in query


def test_queryset_compile_containsall() -> None:
    qs = ModelTest.objects().filter(name__containsall=["a", "b"])
    query = qs._compile_query()
    assert "CONTAINSALL" in query
    assert "name CONTAINSALL ['a', 'b']" in query


def test_queryset_compile_containsany() -> None:
    qs = ModelTest.objects().filter(name__containsany=["a", "b"])
    query = qs._compile_query()
    assert "CONTAINSANY" in query
    assert "name CONTAINSANY ['a', 'b']" in query


def test_queryset_compile_not_in() -> None:
    qs = ModelTest.objects().filter(age__not_in=[1, 2])
    query = qs._compile_query()
    assert "NOT IN" in query
    assert "age NOT IN [1, 2]" in query


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
