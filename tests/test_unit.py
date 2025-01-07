import pytest
from pydantic import ConfigDict, Field
from src import surreal_orm


@pytest.fixture(scope="module", autouse=True)
def setup_model() -> surreal_orm.BaseSurrealModel:
    class TestModel(surreal_orm.BaseSurrealModel):
        model_config = ConfigDict(extra="allow")
        id: str = Field(...)
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0)

    return TestModel(id="1", name="Test", age=45)


@pytest.mark.filterwarnings("ignore:fields may not")
def test_model_get_query_set(setup_model: surreal_orm.BaseSurrealModel) -> None:
    query = setup_model.objects()
    assert isinstance(query, surreal_orm.QuerySet)


def test_model_get_id(setup_model: surreal_orm.BaseSurrealModel) -> None:
    assert setup_model.get_id() == "1"  # cover _data.get("id") is True


def test_model_to_db_dict(setup_model: surreal_orm.BaseSurrealModel) -> None:
    assert setup_model.to_db_dict() == {"name": "Test", "age": 45}


def test_queryset_select() -> None:
    qs = surreal_orm.BaseSurrealModel.objects().select("id", "name")
    assert qs.select_item == ["id", "name"]


def test_queryset_filter() -> None:
    qs = surreal_orm.BaseSurrealModel.objects().filter(name="Test", age__gt=18)
    assert qs._filters == [("name", "exact", "Test"), ("age", "gt", 18)]
    qs = surreal_orm.BaseSurrealModel.objects().filter(name__in=["Test", "Test2"], age__gte=18)
    qs = surreal_orm.BaseSurrealModel.objects().filter(age__lte=45)
    assert qs._filters == [("age", "lte", 45)]
    qs = surreal_orm.BaseSurrealModel.objects().filter(age__lt=45)
    assert qs._filters == [("age", "lt", 45)]


def test_queryset_variables(setup_model: surreal_orm.BaseSurrealModel) -> None:
    qs = setup_model.objects().variables(name="Test")
    assert qs._variables == {"name": "Test"}


def test_queryset_limit(setup_model: surreal_orm.BaseSurrealModel) -> None:
    qs = setup_model.objects().limit(100)
    assert qs._limit == 100


def test_queryset_offset(setup_model: surreal_orm.BaseSurrealModel) -> None:
    qs = setup_model.objects().offset(100)
    assert qs._offset == 100


def test_queryset_order_by(setup_model: surreal_orm.BaseSurrealModel) -> None:
    qs = setup_model.objects().order_by("name")
    assert qs._order_by == "name ASC"


def test_getattr(setup_model: surreal_orm.BaseSurrealModel) -> None:
    assert setup_model.name == "Test"
    assert setup_model.age == 45
    assert setup_model.id == "1"

    with pytest.raises(AttributeError) as exc:
        setup_model.no_attribut

    assert str(exc.value) == "'TestModel' object has no attribute 'no_attribut'."


def test_str_dunnder(setup_model: surreal_orm.BaseSurrealModel) -> None:
    assert str(setup_model) == "{'id': '1', 'name': 'Test', 'age': 45}"


@pytest.mark.filterwarnings("ignore:fields may not")
def test_class_without_config(setup_model: surreal_orm.BaseSurrealModel) -> None:
    class TestModel2(surreal_orm.BaseSurrealModel):
        id: str = Field(...)
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0)

    assert TestModel2(id="1", name="Test", age=45).to_db_dict() == {
        "name": "Test",
        "age": 45,
    }


@pytest.mark.filterwarnings("ignore:fields may not")
def test_class_with_key_specify(setup_model: surreal_orm.BaseSurrealModel) -> None:
    class TestModel3(surreal_orm.BaseSurrealModel):
        model_config = ConfigDict(extra="allow", primary_key="email")  # type: ignore
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0)
        email: str = Field(..., max_length=100, alias="id")

    model = TestModel3(name="Test", age=45, email="test@test.com")  # type: ignore

    assert model.get_id() == "test@test.com"  # type: ignore
