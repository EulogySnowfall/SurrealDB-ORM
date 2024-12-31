import pytest
from typing import Type
from pydantic import BaseModel, ConfigDict, Field
from src.surreal_orm import BaseSurrealModel, SurrealDBConnectionManager, RecordID


SURREALDB_URL = "http://localhost:8000"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb():
    class TestDTO(BaseModel):
        model_config = ConfigDict(extra="allow")
        id: str | RecordID
        name: str = Field(..., max_length=100)

    class TestModel(BaseSurrealModel):
        @classmethod
        def _pydantic_model(cls):
            return TestDTO

    # Initialiser SurrealDB
    SurrealDBConnectionManager(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )

    # Retourne TestModel pour chaque test
    yield TestModel


@pytest.mark.asyncio
async def test_save_model(setup_surrealdb: Type[BaseSurrealModel]):
    model = setup_surrealdb(id="1", name="Test")
    await model.save()

    # Vérification de l'insertion
    client = await SurrealDBConnectionManager().get_client()
    result = await client.select(
        "TestModel"
    )  # Nom de table en minuscule (SurrealDB est sensible à la casse)
    assert len(result) == 1
    assert result[0]["name"] == "Test"


@pytest.mark.asyncio
async def test_query_model(setup_surrealdb: Type[BaseSurrealModel]):
    # Utiliser test_model pour exécuter la requête
    results = await setup_surrealdb.objects().filter(name="Test").exec()
    assert len(results) == 1
    assert results[0].name == "Test"


@pytest.mark.asyncio
async def test_delete_table(setup_surrealdb: Type[BaseSurrealModel]):
    # Suppression de la table via test_model
    result = await setup_surrealdb.objects().delete_table()
    assert result is True
