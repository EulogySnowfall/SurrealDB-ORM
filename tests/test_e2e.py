import pytest
from typing import Type, Generator
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from src import surreal_orm


SURREALDB_URL = "http://localhost:8000"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> Generator[Type[surreal_orm.BaseSurrealModel], None, None]:
    class TestDTO(BaseModel):
        model_config = ConfigDict(extra="allow")
        id: str | surreal_orm.RecordID = Field(...)
        name: str = Field(..., max_length=100)

    class TestModel(surreal_orm.BaseSurrealModel):
        @classmethod
        def _pydantic_model(cls) -> Type[TestDTO]:
            return TestDTO

    # Initialiser SurrealDB
    surreal_orm.SurrealDBConnectionManager(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )

    # Retourne TestModel pour chaque test
    yield TestModel


@pytest.mark.asyncio
async def test_save_model(setup_surrealdb: Type[surreal_orm.BaseSurrealModel]) -> None:
    try:
        model = setup_surrealdb(id="1", name="Test")
        await model.save()
    except ValidationError as exc:
        print(repr(exc.errors()[0]["type"]))

    # Vérification de l'insertion
    client = await surreal_orm.SurrealDBConnectionManager().get_client()
    result = await client.select("TestModel")
    assert len(result) == 1
    assert result[0]["name"] == "Test"


@pytest.mark.asyncio
async def test_query_model(setup_surrealdb: Type[surreal_orm.BaseSurrealModel]) -> None:
    # Utiliser test_model pour exécuter la requête
    results = await setup_surrealdb.objects().filter(name="Test").exec()
    assert len(results) == 1
    assert results[0].name == "Test"


@pytest.mark.asyncio
async def test_delete_table(setup_surrealdb: Type[surreal_orm.BaseSurrealModel]) -> None:
    # Suppression de la table via test_model
    result = await setup_surrealdb.objects().delete_table()
    assert result is True
