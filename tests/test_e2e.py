import pytest
from pydantic import ConfigDict, Field
from src import surreal_orm
from surrealdb import RecordID
from surrealdb.errors import SurrealDbError


SURREALDB_URL = "http://localhost:8000"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


class ModelTest(surreal_orm.BaseSurrealModel):
    model_config = ConfigDict(extra="allow")
    id: str | RecordID | None = Field(default=None)
    name: str = Field(..., max_length=100)
    age: int = Field(..., ge=0, le=125)


class ModelTestEmpty(surreal_orm.BaseSurrealModel):
    model_config = ConfigDict(extra="allow")
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


@pytest.mark.filterwarnings("ignore:fields may not")
async def test_save_model() -> None:
    model = ModelTest(id="1", name="Test", age=32)
    await model.save()

    # Vérification de l'insertion
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    result = await client.select("ModelTest")
    test_id = RecordID(table_name="ModelTest", identifier=1)
    assert len(result) == 1

    assert result[0] == {"id": test_id, "name": "Test", "age": 32}


async def test_merge_model() -> None:
    item = await ModelTest.objects().get(1)
    assert item.name == "Test"
    assert item.age == 32
    await item.merge(age=23)  # Also test whole refresh() method
    item.age = 23
    item.name = "Test"
    item.id = "1"

    item2 = await ModelTest.objects().filter(name="Test").get()
    assert item2.age == 23
    assert item2.name == "Test"
    assert item2.id == "1"


async def test_update_model() -> None:
    item = await ModelTest.objects().get(1)
    assert item.name == "Test"
    assert item.age == 23
    item.age = 25
    await item.update()
    item2 = await ModelTest.objects().get(1)
    assert item2.age == 25

    item2 = await ModelTest.objects().filter(name="Test").get()
    assert item2.age == 25
    assert item2.name == "Test"
    assert item2.id == "1"

    item3 = ModelTest(id=None, name="TestNone", age=17)

    with pytest.raises(SurrealDbError) as exc1:
        await item3.update()

    assert str(exc1.value) == "Can't update data, no id found."

    with pytest.raises(SurrealDbError) as exc2:
        await item3.merge(age=19)

    assert str(exc2.value) == "No Id for the data to merge: {'age': 19}"


async def test_first_model() -> None:
    model = await ModelTest.objects().filter(name="Test").first()
    if isinstance(model, ModelTest):
        assert model.name == "Test"
        assert model.age == 25
        assert model.id == "1"
    else:
        assert False

    with pytest.raises(SurrealDbError) as exc1:
        await ModelTest.objects().filter(name="NotExist").first()

    assert str(exc1.value) == "No result found."


async def test_filter_model() -> None:
    item3 = ModelTest(id=None, name="Test2", age=17)
    await item3.save()

    models = await ModelTest.objects().filter(age__lt=30).exec()  # Test from_db isinstance(record["id"], RecordID)
    assert len(models) == 2
    for model in models:
        assert model.age < 30


async def test_delete_model() -> None:
    model = ModelTest(id="2", name="Test2", age=34)
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
    results = await ModelTest.objects().filter(name="Test").exec()
    assert len(results) == 1
    assert results[0].name == "Test"


async def test_multi_select() -> None:
    await ModelTest(id=None, name="Ian", age=23).save()
    await ModelTest(id=None, name="Yan", age=32).save()
    await ModelTest(id=None, name="Isa", age=32).save()

    result = await ModelTest.objects().all()

    assert len(result) == 5

    result2 = await ModelTest.objects().filter(name__in=["Ian", "Yan"]).exec()

    assert len(result2) == 2
    for item in result2:
        assert item.name in ["Yan", "Ian"]


async def test_order_by() -> None:
    result1 = await ModelTest.objects().order_by("name").exec()
    assert len(result1) == 5
    assert result1[0].name == "Ian"

    result2 = await ModelTest.objects().order_by("name", surreal_orm.OrderBy.DESC).exec()
    assert len(result2) == 5
    assert result2[0].name == "Yan"


async def test_offset() -> None:
    result = await ModelTest.objects().offset(2).exec()
    assert len(result) == 3


async def test_limit() -> None:
    result = await ModelTest.objects().limit(2).exec()
    assert len(result) == 2


async def test_select_field() -> None:
    result = await ModelTest.objects().select("name", "age").exec()
    assert len(result) == 5
    assert isinstance(result[0], dict)


async def test_select_with_variable() -> None:
    result = await ModelTest.objects().filter(age__lte="$max_age").variables(max_age=25).exec()
    assert len(result) == 3
    for res in result:
        assert res.age <= 25


async def test_query() -> None:
    result = await ModelTest.objects().query("SELECT * FROM ModelTest WHERE age > 25")
    assert len(result) == 2
    for res in result:
        assert res.age > 25

    result2 = await ModelTest.objects().query("SELECT * FROM ModelTest WHERE age > $age", {"age": 19})
    assert len(result2) == 4

    with pytest.raises(SurrealDbError) as exc:
        await ModelTest.objects().query("SELECT * FROM NoTable WHERE age > 34")

    assert str(exc.value) == "The query must include 'FROM ModelTest' to reference the correct table."


async def test_error_on_get_multi() -> None:
    with pytest.raises(SurrealDbError) as exc1:
        await ModelTest.objects().get()

    assert str(exc1.value) == "More than one result found."

    with pytest.raises(SurrealDbError) as exc2:
        await ModelTestEmpty.objects().get()

    assert str(exc2.value) == "No result found."


@pytest.mark.filterwarnings("ignore:fields may not")
async def test_with_primary_key() -> None:
    class ModelTest2(surreal_orm.BaseSurrealModel):
        model_config = ConfigDict(extra="allow", primary_key="email")  # type: ignore
        name: str = Field(..., max_length=100)
        age: int = Field(..., ge=0, le=125)
        email: str = Field(..., max_length=100)

    model = ModelTest2(name="Test", age=32, email="test@test.com")
    await model.save()

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
