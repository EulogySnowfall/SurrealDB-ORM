"""
Tests for the testing module — SurrealFixture, ModelFactory, Faker.

Unit tests (no DB required) + integration tests (marked with @pytest.mark.integration).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import date, datetime
from typing import Any

import pytest

import surreal_orm
from surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from surreal_orm.testing import Faker, ModelFactory, SurrealFixture, fixture
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class SimpleModel(BaseSurrealModel):
    id: str | None = None
    name: str
    age: int = 0
    role: str = "user"


class ProductModel(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="test_products")
    id: str | None = None
    name: str
    price: float = 0.0
    category: str = "general"


# ---------------------------------------------------------------------------
# Faker
# ---------------------------------------------------------------------------


class TestFaker:
    """Tests for Faker provider."""

    def test_name(self) -> None:
        value = Faker("name").generate()
        assert isinstance(value, str)
        assert " " in value  # "First Last"

    def test_first_name(self) -> None:
        value = Faker("first_name").generate()
        assert isinstance(value, str)
        assert len(value) > 0

    def test_last_name(self) -> None:
        value = Faker("last_name").generate()
        assert isinstance(value, str)
        assert len(value) > 0

    def test_email(self) -> None:
        value = Faker("email").generate()
        assert isinstance(value, str)
        assert "@" in value
        assert "." in value

    def test_random_int(self) -> None:
        value = Faker("random_int", min=10, max=20).generate()
        assert isinstance(value, int)
        assert 10 <= value <= 20

    def test_random_int_defaults(self) -> None:
        value = Faker("random_int").generate()
        assert isinstance(value, int)
        assert 0 <= value <= 100

    def test_random_float(self) -> None:
        value = Faker("random_float", min=1.0, max=5.0).generate()
        assert isinstance(value, float)
        assert 1.0 <= value <= 5.0

    def test_text(self) -> None:
        value = Faker("text", max_length=50).generate()
        assert isinstance(value, str)
        assert len(value) <= 50

    def test_sentence(self) -> None:
        value = Faker("sentence").generate()
        assert isinstance(value, str)
        assert value.endswith(".")

    def test_word(self) -> None:
        value = Faker("word").generate()
        assert isinstance(value, str)
        assert " " not in value

    def test_uuid(self) -> None:
        value = Faker("uuid").generate()
        assert isinstance(value, str)
        assert len(value) == 36  # UUID4 format

    def test_boolean(self) -> None:
        value = Faker("boolean").generate()
        assert isinstance(value, bool)

    def test_date(self) -> None:
        value = Faker("date").generate()
        assert isinstance(value, date)

    def test_datetime(self) -> None:
        value = Faker("datetime").generate()
        assert isinstance(value, datetime)

    def test_choice(self) -> None:
        items = ["a", "b", "c"]
        value = Faker("choice", items=items).generate()
        assert value in items

    def test_choice_missing_items(self) -> None:
        with pytest.raises(ValueError, match="requires 'items'"):
            Faker("choice").generate()

    def test_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown Faker provider"):
            Faker("nonexistent").generate()

    def test_repr(self) -> None:
        f = Faker("random_int", min=1, max=10)
        assert "random_int" in repr(f)
        assert "min=1" in repr(f)

    def test_repr_no_kwargs(self) -> None:
        f = Faker("name")
        assert repr(f) == "Faker('name')"


# ---------------------------------------------------------------------------
# ModelFactory
# ---------------------------------------------------------------------------


class SimpleFactory(ModelFactory):
    class Meta:
        model = SimpleModel

    name = Faker("name")
    age = Faker("random_int", min=18, max=80)
    role = "player"


class ProductFactory(ModelFactory):
    class Meta:
        model = ProductModel

    name = Faker("word")
    price = Faker("random_float", min=1.0, max=100.0)
    category = Faker("choice", items=["electronics", "books", "clothing"])


class TestModelFactory:
    """Tests for ModelFactory (unit — no DB)."""

    def test_build(self) -> None:
        user = SimpleFactory.build()
        assert isinstance(user, SimpleModel)
        assert isinstance(user.name, str)
        assert 18 <= user.age <= 80
        assert user.role == "player"

    def test_build_with_override(self) -> None:
        user = SimpleFactory.build(role="admin", age=99)
        assert user.role == "admin"
        assert user.age == 99

    def test_build_batch(self) -> None:
        users = SimpleFactory.build_batch(5)
        assert len(users) == 5
        assert all(isinstance(u, SimpleModel) for u in users)
        # Random data should produce at least some variation
        names = {u.name for u in users}
        assert len(names) >= 1  # Could be same by random chance, but unlikely to be all

    def test_build_product(self) -> None:
        product = ProductFactory.build()
        assert isinstance(product, ProductModel)
        assert isinstance(product.name, str)
        assert 1.0 <= product.price <= 100.0
        assert product.category in ["electronics", "books", "clothing"]

    def test_missing_meta_model(self) -> None:
        class BadFactory(ModelFactory):
            class Meta:
                pass

            name = "test"

        with pytest.raises(ValueError, match="Meta.model is not set"):
            BadFactory.build()

    def test_extra_override_field(self) -> None:
        """Overrides for fields not in _field_defs should pass through."""
        user = SimpleFactory.build(id="custom-id")
        assert user.id == "custom-id"


# ---------------------------------------------------------------------------
# SurrealFixture
# ---------------------------------------------------------------------------


@fixture
class UserFixtures(SurrealFixture):
    alice = SimpleModel(name="Alice", age=30, role="admin")
    bob = SimpleModel(name="Bob", age=25, role="player")


class TestSurrealFixture:
    """Tests for SurrealFixture (unit — no DB)."""

    def test_fixture_instances_discovered(self) -> None:
        assert "alice" in UserFixtures._fixture_instances
        assert "bob" in UserFixtures._fixture_instances
        assert len(UserFixtures._fixture_instances) == 2

    def test_fixture_instance_types(self) -> None:
        assert isinstance(UserFixtures._fixture_instances["alice"], SimpleModel)
        assert UserFixtures._fixture_instances["alice"].name == "Alice"

    async def test_no_decorator_raises(self) -> None:
        class BadFixture(SurrealFixture):
            pass

        with pytest.raises(ValueError, match="no fixture instances"):
            async with BadFixture.load():
                pass


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

SURREALDB_DATABASE = "test"


@pytest.fixture(scope="module")
async def setup_testing_db() -> AsyncGenerator[None, Any]:
    """Setup connection for integration tests (not autouse — only pulled by integration tests)."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    for table in ["SimpleModel", "test_products"]:
        await client.query(f"REMOVE TABLE IF EXISTS {table};")

    yield

    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    for table in ["SimpleModel", "test_products"]:
        await client.query(f"REMOVE TABLE IF EXISTS {table};")


@pytest.mark.integration
async def test_factory_create(setup_testing_db: None) -> None:
    """Factory.create() saves to DB and returns instance with ID."""
    user = await SimpleFactory.create()
    assert user.get_id() is not None
    assert isinstance(user.name, str)

    # Verify it's in the DB
    fetched = await SimpleModel.objects().get(user.get_id())
    assert fetched.name == user.name

    # Cleanup
    await user.delete()


@pytest.mark.integration
async def test_factory_create_batch(setup_testing_db: None) -> None:
    """Factory.create_batch() creates multiple records."""
    users = await SimpleFactory.create_batch(3)
    assert len(users) == 3
    assert all(u.get_id() is not None for u in users)

    # Verify count in DB
    all_users = await SimpleModel.objects().all()
    assert len(all_users) >= 3

    # Cleanup
    for u in users:
        await u.delete()


@pytest.mark.integration
async def test_fixture_load_unload(setup_testing_db: None) -> None:
    """Fixture loads instances and cleans up on exit."""
    async with UserFixtures.load() as fixtures:
        assert fixtures.alice.get_id() is not None
        assert fixtures.bob.get_id() is not None

        # Verify in DB
        all_users = await SimpleModel.objects().all()
        assert len(all_users) >= 2

    # After context exit, records should be deleted
    remaining = await SimpleModel.objects().all()
    alice_found = any(u.name == "Alice" for u in remaining)
    assert not alice_found, "Fixture cleanup should have deleted Alice"


@pytest.mark.integration
async def test_query_logger_captures_real_queries(setup_testing_db: None) -> None:
    """QueryLogger captures queries from real DB operations."""
    from surreal_orm.debug import QueryLogger

    user = SimpleFactory.build(name="LoggerTest")

    async with QueryLogger() as logger:
        await user.save()
        await SimpleModel.objects().filter(name="LoggerTest").exec()
        await user.delete()

    assert logger.total_queries >= 3
    assert logger.total_ms > 0
    # At least one query should be a SELECT
    sql_strs = [q.sql for q in logger.queries]
    assert any("SELECT" in s for s in sql_strs)
