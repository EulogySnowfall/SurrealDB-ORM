import pytest
from typing import AsyncGenerator, Any
from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.connection_manager import SurrealDbConnectionError

SURREALDB_URL = "http://localhost:8000"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "ns"
SURREALDB_DATABASE = "db"


@pytest.fixture
async def setup_connection_manager() -> AsyncGenerator[Any, Any]:
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    yield
    await SurrealDBConnectionManager.close_connection()


def test_set_connection() -> None:
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    assert SurrealDBConnectionManager.is_connection_set() is True


@pytest.mark.integration
async def test_get_client(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    client = await SurrealDBConnectionManager.get_client()
    assert client is not None
    assert SurrealDBConnectionManager.is_connected() is True
    await SurrealDBConnectionManager.close_connection()
    assert SurrealDBConnectionManager.is_connected() is False
    await SurrealDBConnectionManager.set_user("wrong_user")

    with pytest.raises(SurrealDbConnectionError) as exc1:
        await SurrealDBConnectionManager.get_client()

    await SurrealDBConnectionManager.unset_connection()
    with pytest.raises(ValueError) as exc2:
        await SurrealDBConnectionManager.get_client()

    assert str(exc1.value).startswith("Can't connect to the database")
    assert str(exc2.value) == "Connection not been set."


@pytest.mark.integration
async def test_close_connection() -> None:
    await SurrealDBConnectionManager.close_connection()
    assert SurrealDBConnectionManager.is_connected() is False


@pytest.mark.integration
async def test_get_connection_string(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    connection_string = SurrealDBConnectionManager.get_connection_string()
    assert connection_string == SURREALDB_URL


@pytest.mark.integration
def test_get_connection_kwargs(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    kwargs = SurrealDBConnectionManager.get_connection_kwargs()
    assert kwargs == {
        "url": SURREALDB_URL,
        "user": SURREALDB_USER,
        "namespace": SURREALDB_NAMESPACE,
        "database": SURREALDB_DATABASE,
    }


@pytest.mark.integration
def test_is_connection_set(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    kwargs = SurrealDBConnectionManager.is_connection_set()
    assert kwargs is True


@pytest.mark.integration
async def test_set_url(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    new_url = "http://localhost:8001"
    assert await SurrealDBConnectionManager.set_url(new_url) is True
    assert SurrealDBConnectionManager.get_url() == new_url
    assert await SurrealDBConnectionManager.set_url(new_url, True) is False  # Cover validate_connection to False

    await SurrealDBConnectionManager.unset_connection()
    with pytest.raises(ValueError) as exc:
        await SurrealDBConnectionManager.set_url(new_url)

    assert str(exc.value) == "You can't change the URL when the others setting are not already set."


@pytest.mark.integration
async def test_set_user(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    new_user = "admin"
    assert await SurrealDBConnectionManager.set_user(new_user) is True
    assert SurrealDBConnectionManager.get_user() == new_user
    assert await SurrealDBConnectionManager.set_user(new_user, True) is False  # Cover validate_connection to False
    assert SurrealDBConnectionManager.get_user() is None
    await SurrealDBConnectionManager.unset_connection()
    with pytest.raises(ValueError) as exc:
        await SurrealDBConnectionManager.set_user(new_user)

    assert str(exc.value) == "You can't change the User when the others setting are not already set."


@pytest.mark.integration
async def test_set_password(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    new_password = "new_pass"
    assert await SurrealDBConnectionManager.set_password(new_password) is True
    assert SurrealDBConnectionManager.is_password_set()
    assert await SurrealDBConnectionManager.set_password(new_password, True) is False  # Cover validate_connection to False
    assert SurrealDBConnectionManager.is_password_set() is False
    assert SurrealDBConnectionManager.is_connection_set() is False

    await SurrealDBConnectionManager.unset_connection()
    with pytest.raises(ValueError) as exc:
        await SurrealDBConnectionManager.set_password(new_password)

    assert str(exc.value) == "You can't change the password when the others setting are not already set."


@pytest.mark.integration
async def test_set_namespace(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    new_namespace = "new_ns"
    assert await SurrealDBConnectionManager.set_namespace(new_namespace) is True
    assert SurrealDBConnectionManager.get_namespace() == new_namespace
    assert await SurrealDBConnectionManager.set_namespace(new_namespace, True) is True
    await SurrealDBConnectionManager.set_password("wrong_pass")
    assert await SurrealDBConnectionManager.set_namespace(new_namespace, True) is False  # Cover validate_connection to False

    await SurrealDBConnectionManager.unset_connection()
    with pytest.raises(ValueError) as exc:
        await SurrealDBConnectionManager.set_namespace(new_namespace)

    assert str(exc.value) == "You can't change the namespace when the others setting are not already set."


@pytest.mark.integration
async def test_set_database(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    new_database = "new_db"
    assert await SurrealDBConnectionManager.set_database(new_database) is True
    assert SurrealDBConnectionManager.get_database() == new_database
    assert await SurrealDBConnectionManager.set_database(new_database, True) is True
    await SurrealDBConnectionManager.set_password("wrong_pass")
    assert await SurrealDBConnectionManager.set_database(new_database, True) is False  # Cover validate_connection to False

    await SurrealDBConnectionManager.unset_connection()
    with pytest.raises(ValueError) as exc:
        await SurrealDBConnectionManager.set_database(new_database)

    assert str(exc.value) == "You can't change the database when the others setting are not already set."


@pytest.mark.integration
async def test_unset() -> None:
    await SurrealDBConnectionManager.unset_connection()
    assert SurrealDBConnectionManager.is_connected() is False
    assert SurrealDBConnectionManager.is_connection_set() is False


@pytest.mark.integration
async def test_reconnect(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    await SurrealDBConnectionManager.close_connection()
    assert SurrealDBConnectionManager.is_connected() is False
    await SurrealDBConnectionManager.reconnect()
    assert SurrealDBConnectionManager.is_connected() is True


@pytest.mark.integration
async def test_context_manager(setup_connection_manager: AsyncGenerator[Any, Any]) -> None:
    async with SurrealDBConnectionManager():
        assert SurrealDBConnectionManager.is_connected() is True

    assert SurrealDBConnectionManager.is_connected() is False
