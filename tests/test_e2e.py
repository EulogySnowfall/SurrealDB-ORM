import pytest
from pydantic import Field
from src import surreal_orm
from surrealdb import RecordID
from surrealdb.errors import SurrealDbError, SurrealDbConnectionError


SURREALDB_URL = "http://localhost:8000"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


class ModelTest(surreal_orm.BaseSurrealModel):
    id: str | RecordID | None = None
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0, le=125)


class ModelTestEmpty(surreal_orm.BaseSurrealModel):
    id: str | RecordID | None = Field(default=None)
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0, le=125)


@pytest.fixture(scope="module", autouse=True)
def setup_surrealdb() -> None:
    # Initialiser SurrealDB
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )


async def test_save_model() -> None:
    model = ModelTest(id="1", name="Test Man", age=42)
    await model.save()

    # Vérification de l'insertion
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    result = await client.select("ModelTest")
    test_id = RecordID(table_name="ModelTest", identifier=1)
    assert len(result) == 1

    assert result[0] == {"id": test_id, "name": "Test Man", "age": 42}


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


async def test_filter_model() -> None:
    item3 = ModelTest(name="Test2", age=17)
    await item3.save()

    models = await ModelTest.objects().filter(age__lt=30).exec()  # Test from_db isinstance(record["id"], RecordID)
    assert len(models) == 2
    for model in models:
        assert model.age < 30


async def test_save_model_already_exist() -> None:
    model = ModelTest(id="1", name="Test2", age=34)
    with pytest.raises(SurrealDbError) as exc:
        await model.save()

    assert str(exc.value) == "There was a problem with the database: Database record `ModelTest:⟨1⟩` already exists"


async def test_delete_model() -> None:
    model = ModelTest(id="4", name="Test2", age=34)
    await model.save()
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    result = await client.select("ModelTest")
    assert len(result) == 3

    await model.delete()
    result = await client.select("ModelTest")
    assert len(result) == 2

    model2 = ModelTest(id="345", name="Test2", age=34)

    with pytest.raises(SurrealDbError) as exc1:
        await model2.delete()  # Test delete() without saved()

    assert str(exc1.value) == "Can't delete Record id -> '345' not found!"


async def test_query_model() -> None:
    # Utiliser test_model pour exécuter la requête
    results = await ModelTest.objects().filter(name="Test Man").exec()
    assert len(results) == 1
    assert results[0].name == "Test Man"


async def test_multi_select() -> None:
    await ModelTest(name="Ian", age=23).save()
    await ModelTest(name="Yan", age=32).save()
    await ModelTest(name="Isa", age=32).save()

    result = await ModelTest.objects().all()

    assert len(result) == 5

    result1 = await ModelTest.objects().filter(name__in=["Ian", "Yan"]).exec()

    assert len(result1) == 2
    for item in result1:
        assert item.name in ["Yan", "Ian"]

    # Test order_by
    result2 = await ModelTest.objects().order_by("name").exec()
    assert len(result2) == 5
    assert result2[0].name == "Ian"

    # Test order_by DESC
    result3 = await ModelTest.objects().order_by("name", surreal_orm.OrderBy.DESC).exec()
    assert len(result3) == 5
    assert result3[0].name == "Yan"

    # Test offset and limit
    result4 = await ModelTest.objects().offset(2).exec()
    assert len(result4) == 3

    result5 = await ModelTest.objects().limit(2).exec()
    assert len(result5) == 2

    # Select only age
    result6 = await ModelTest.objects().select("age").exec()
    assert len(result6) == 5
    assert isinstance(result6[0], dict)

    result7 = await ModelTest.objects().filter(age__lte="$max_age").variables(max_age=25).exec()
    assert len(result7) == 3
    for res in result7:
        assert res.age <= 25

    result8 = await ModelTest.objects().query("SELECT * FROM ModelTest WHERE age > 25")
    assert len(result8) == 2
    for res in result8:
        assert res.age > 25

    result9 = await ModelTest.objects().query("SELECT * FROM ModelTest WHERE age > $age", {"age": 19})
    assert len(result9) == 4

    with pytest.raises(SurrealDbError) as exc:
        await ModelTest.objects().query("SELECT * FROM NoTable WHERE age > 34")

    assert str(exc.value) == "The query must include 'FROM ModelTest' to reference the correct table."


async def test_error_on_get_multi() -> None:
    with pytest.raises(SurrealDbError) as exc1:
        await ModelTest.objects().get()

    assert str(exc1.value) == "More than one result found."

    with pytest.raises(ModelTestEmpty.DoesNotExist) as exc2:
        await ModelTestEmpty.objects().get()

    assert str(exc2.value) == "Record not found."


async def test_with_primary_key() -> None:
    class ModelTest2(surreal_orm.BaseSurrealModel):
        model_config = surreal_orm.SurrealConfigDict(primary_key="email")
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0, le=125)
        email: str = Field(..., max_length=100)

    model = ModelTest2(name="Test", age=32, email="test@test.com")
    await model.save()

    # Error on duplicate primary key
    with pytest.raises(SurrealDbConnectionError) as exc:
        await ModelTest2(name="Test3", age=35, email="test@test.com").save()

    assert (
        str(exc.value) == "There was a problem with the database: Database record `ModelTest2:⟨test@test.com⟩` already exists"
    )

    fletch = await ModelTest2.objects().get("test@test.com")
    if isinstance(fletch, ModelTest2):
        assert fletch.name == "Test"
        assert fletch.age == 32
        assert fletch.email == "test@test.com"
    else:
        assert False

    deleted = await ModelTest2.objects().delete_table()
    assert deleted is True


async def test_delete_table() -> None:
    # Suppression de la table via test_model
    result = await ModelTest.objects().delete_table()
    assert result is True
