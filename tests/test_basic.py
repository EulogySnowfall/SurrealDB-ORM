import pytest
from pydantic import BaseModel, ConfigDict, Field
from src import surreal_orm


@pytest.fixture(scope="module", autouse=True)
def setup_model() -> surreal_orm.BaseSurrealModel:
    class TestDTO(BaseModel):
        model_config = ConfigDict(extra="allow")
        id: str = Field(...)
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0)

    class TestModel(surreal_orm.BaseSurrealModel):
        @classmethod
        def _pydantic_model(cls) -> type[TestDTO]:
            return TestDTO

    return TestModel(id="1", name="Test", age=45)


def test_model_get_id(setup_model: surreal_orm.BaseSurrealModel) -> None:
    assert setup_model.get_id() == "1"


def test_model_to_db_dict(setup_model: surreal_orm.BaseSurrealModel) -> None:
    assert setup_model.to_db_dict() == {"id": "1", "name": "Test", "age": 45}


def test_queryset_select() -> None:
    qs = surreal_orm.BaseSurrealModel.objects().select("id", "name")
    assert qs.select_item == ["id", "name"]


def test_queryset_filter() -> None:
    qs = surreal_orm.BaseSurrealModel.objects().filter(name="Test")
    assert qs._filters == [("name", "exact", "Test")]
