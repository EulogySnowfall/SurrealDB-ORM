from typing import Any, AsyncGenerator
import pytest
from pydantic import Field
from src import surreal_orm
from src.surreal_orm.model_base import SurrealDbError
from src.surreal_orm.query_set import SurrealDbError as QuerySetError
from tests.conftest import SURREALDB_URL, SURREALDB_USER, SURREALDB_PASS, SURREALDB_NAMESPACE

SURREALDB_DATABASE = "test_e2e"


class ModelTest(surreal_orm.BaseSurrealModel):
    id: str | None = None
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0, le=125)


class ModelTestEmpty(surreal_orm.BaseSurrealModel):
    id: str | None = Field(default=None)
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0, le=125)


@pytest.fixture(scope="module", autouse=True)
async def setup_and_clean_surrealdb() -> AsyncGenerator[None, Any]:
    """Initialize SurrealDB connection, clean database before and after tests."""
    # Initialize SurrealDB connection
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )

    # Setup: Remove and recreate test database for clean state
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    try:
        # We can't REMOVE DATABASE we're connected to, so delete all tables
        for table in ["ModelTest", "ModelTestEmpty", "CustomModelName"]:
            try:
                await client.query(f"REMOVE TABLE IF EXISTS {table};")
            except Exception:
                pass
    except Exception:
        pass

    yield  # Run tests

    # Teardown: Clean up all tables
    try:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        for table in ["ModelTest", "ModelTestEmpty", "CustomModelName"]:
            try:
                await client.query(f"REMOVE TABLE IF EXISTS {table};")
            except Exception:
                pass
    except Exception:
        pass


@pytest.mark.integration
async def test_save_model() -> None:
    model = ModelTest(id="1", name="Test Man", age=42)
    await model.save()

    # Vérification de l'insertion
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    result = await client.select("ModelTest")
    # SDK returns RecordsResponse
    assert result.count == 1
    record = result.first
    assert record is not None
    assert record["name"] == "Test Man"
    assert record["age"] == 42


@pytest.mark.integration
async def test_merge_model() -> None:
    item = await ModelTest.objects().get("1")
    assert item.name == "Test Man"
    assert item.age == 42
    await item.merge(age=32)  # Also test whole refresh() method
    item.age = 32
    item.name = "Test Man"
    item.id = "1"

    item2 = await ModelTest.objects().filter(name="Test Man").get()
    assert item2.age == 32
    assert item2.name == "Test Man"
    assert item2.id == "1"


@pytest.mark.integration
async def test_update_model() -> None:
    item = await ModelTest.objects().get("1")
    assert item.name == "Test Man"
    assert item.age == 32
    item.age = 25
    await item.update()
    item2 = await ModelTest.objects().get("1")
    assert item2.age == 25

    item2 = await ModelTest.objects().filter(name="Test Man").get()
    assert item2.age == 25
    assert item2.name == "Test Man"
    assert item2.id == "1"

    item3 = ModelTest(name="TestNone", age=17)

    with pytest.raises(SurrealDbError) as exc1:
        await item3.update()

    assert str(exc1.value) == "Can't update data, no id found."

    with pytest.raises(SurrealDbError) as exc2:
        await item3.refresh()

    assert str(exc2.value) == "Can't refresh data, not recorded yet."  # test Error in refresh()

    with pytest.raises(SurrealDbError) as exc2:
        await item3.merge(age=19)

    assert str(exc2.value) == "No Id for the data to merge: {'age': 19}"

    with pytest.raises(ModelTest.DoesNotExist) as exc3:
        await ModelTest.objects().get("NotExist")

    assert str(exc3.value) == "Record not found."


@pytest.mark.integration
async def test_first_model() -> None:
    model = await ModelTest.objects().filter(name="Test Man").first()
    if isinstance(model, ModelTest):
        assert model.name == "Test Man"
        assert model.age == 25
        assert model.id == "1"
    else:
        assert False

    with pytest.raises(ModelTest.DoesNotExist) as exc1:
        result = await ModelTest.objects().filter(name="NotExist").first()

        print(result)
    assert str(exc1.value) == "Query returned no results."


@pytest.mark.integration
async def test_filter_model() -> None:
    item3 = ModelTest(name="Test2", age=17)
    await item3.save()

    models = await ModelTest.objects().filter(age__lt=30).exec()  # Test from_db isinstance(record["id"], RecordID)
    assert len(models) == 2
    for model in models:
        assert model.age < 30


@pytest.mark.integration
async def test_save_model_already_exist() -> None:
    """Test that saving a model with an existing ID raises QueryError."""

    # Create a dedicated model for this test
    class DuplicateTestModel(surreal_orm.BaseSurrealModel):
        id: str | None = None
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0, le=125)

    # Clean up and create initial record
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query("DELETE DuplicateTestModel")

    # First save should succeed
    model1 = DuplicateTestModel(id="dup1", name="Test1", age=30)
    await model1.save()

    # Second save with same ID should perform upsert (update existing record)
    model2 = DuplicateTestModel(id="dup1", name="Test2", age=34)
    await model2.save()

    # Verify the record was updated (upsert behavior)
    result = await client.select("DuplicateTestModel:dup1")
    assert result.count == 1
    assert result.first["name"] == "Test2"
    assert result.first["age"] == 34


@pytest.mark.integration
async def test_delete_model() -> None:
    model = ModelTest(id="4", name="Test2", age=34)
    await model.save()
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    result = await client.select("ModelTest")
    # SDK returns RecordsResponse
    assert result.count == 3

    await model.delete()
    result = await client.select("ModelTest")
    assert result.count == 2

    model2 = ModelTest(id="345", name="Test2", age=34)

    with pytest.raises(SurrealDbError) as exc1:
        await model2.delete()  # Test delete() without saved()

    assert str(exc1.value) == "Can't delete Record id -> '345' not found!"


@pytest.mark.integration
async def test_query_model() -> None:
    # Utiliser test_model pour exécuter la requête
    results = await ModelTest.objects().filter(name="Test Man").exec()
    assert len(results) == 1
    assert results[0].name == "Test Man"


@pytest.mark.integration
async def test_multi_select() -> None:
    # Use a dedicated table for this test to avoid conflicts
    class MultiSelectTest(surreal_orm.BaseSurrealModel):
        id: str | None = None
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0, le=125)

    # Clean up from any previous test runs
    await MultiSelectTest.objects().delete_table()

    await MultiSelectTest(name="Ian", age=23).save()
    await MultiSelectTest(name="Yan", age=32).save()
    await MultiSelectTest(name="Isa", age=32).save()

    result = await MultiSelectTest.objects().all()
    assert len(result) == 3

    result1 = await MultiSelectTest.objects().filter(name__in=["Ian", "Yan"]).exec()
    assert len(result1) == 2
    for item in result1:
        assert item.name in ["Yan", "Ian"]

    # Test order_by
    result2 = await MultiSelectTest.objects().order_by("name").exec()
    assert len(result2) == 3
    assert result2[0].name == "Ian"

    # Test order_by DESC
    result3 = await MultiSelectTest.objects().order_by("name", surreal_orm.OrderBy.DESC).exec()
    assert len(result3) == 3
    assert result3[0].name == "Yan"

    # Test offset and limit
    result4 = await MultiSelectTest.objects().offset(1).exec()
    assert len(result4) == 2

    result5 = await MultiSelectTest.objects().limit(2).exec()
    assert len(result5) == 2

    # Select only age
    result6 = await MultiSelectTest.objects().select("age").exec()
    assert len(result6) == 3
    assert isinstance(result6[0], dict)

    result7 = await MultiSelectTest.objects().filter(age__lte="$max_age").variables(max_age=25).exec()
    assert len(result7) == 1  # Only Ian (age 23)
    for res in result7:
        assert res.age <= 25

    result8 = await MultiSelectTest.objects().query("SELECT * FROM MultiSelectTest WHERE age > 25")
    assert len(result8) == 2  # Yan and Isa (age 32)
    for res in result8:
        assert res.age > 25

    result9 = await MultiSelectTest.objects().query("SELECT * FROM MultiSelectTest WHERE age > $age", {"age": 19})
    assert len(result9) == 3  # All records have age > 19

    with pytest.raises(QuerySetError) as exc:
        await MultiSelectTest.objects().query("SELECT * FROM NoTable WHERE age > 34")

    assert str(exc.value) == "The query must include 'FROM MultiSelectTest' to reference the correct table."


@pytest.mark.integration
async def test_error_on_get_multi() -> None:
    """Test error handling when get() finds multiple records or no records."""

    # Create a dedicated model for this test to avoid conflicts
    class GetMultiTestModel(surreal_orm.BaseSurrealModel):
        id: str | None = None
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0, le=125)

    # Clean up before test
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query("DELETE GetMultiTestModel")

    # Create multiple records
    model1 = GetMultiTestModel(id="1", name="Test1", age=25)
    model2 = GetMultiTestModel(id="2", name="Test2", age=30)
    await model1.save()
    await model2.save()

    # Test that get() with multiple records raises error
    with pytest.raises(QuerySetError) as exc1:
        await GetMultiTestModel.objects().get()

    assert str(exc1.value) == "More than one result found."

    # Test that get() with no records raises DoesNotExist
    await client.query("DELETE GetMultiTestModel")

    with pytest.raises(GetMultiTestModel.DoesNotExist) as exc2:
        await GetMultiTestModel.objects().get()

    assert str(exc2.value) == "Record not found."


@pytest.mark.integration
async def test_with_primary_key() -> None:
    """Test model with custom primary key field."""

    class ModelWithPK(surreal_orm.BaseSurrealModel):
        model_config = surreal_orm.SurrealConfigDict(primary_key="username")
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0, le=125)
        username: str = Field(..., max_length=100)

    # Clean up before test
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query("DELETE ModelWithPK")

    # Create model with custom primary key
    model = ModelWithPK(name="Test", age=32, username="testuser123")
    await model.save()

    # Second save with same primary key should perform upsert (update)
    await ModelWithPK(name="Test3", age=35, username="testuser123").save()

    # Fetch by primary key - should have updated values
    fetched = await ModelWithPK.objects().get("testuser123")
    if isinstance(fetched, ModelWithPK):
        # Values should be updated from the second save
        assert fetched.name == "Test3"
        assert fetched.age == 35
        assert fetched.username == "testuser123"
    else:
        assert False

    # Clean up
    deleted = await ModelWithPK.objects().delete_table()
    assert deleted is True


@pytest.mark.integration
async def test_delete_table() -> None:
    # Suppression de la table via test_model
    result = await ModelTest.objects().delete_table()
    assert result is True
