import pytest
from pydantic import BaseModel, ConfigDict, Field
from src.surreal_orm import BaseSurrealModel


@pytest.fixture
def test_model():

    class TestDTO(BaseModel):
        model_config = ConfigDict(extra="allow")
        id: str
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0)

    class TestModel(BaseSurrealModel):
        @classmethod
        def _pydantic_model(cls):
            return TestDTO

    return TestModel(id="1", name="Test", age=45)


def test_model_get_id(test_model: BaseSurrealModel):
    assert test_model.get_id() == "1"


def test_model_to_db_dict(test_model: BaseSurrealModel):
    assert test_model.to_db_dict() == {"id": "1", "name": "Test", "age": 45}


def test_queryset_select():
    qs = BaseSurrealModel.objects().select("id", "name")
    assert qs.select_item == ["id", "name"]


def test_queryset_filter():
    qs = BaseSurrealModel.objects().filter(name="Test")
    assert qs._filters == [("name", "exact", "Test")]
