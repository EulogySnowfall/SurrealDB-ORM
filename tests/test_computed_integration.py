"""
Integration tests for Computed field type.

Tests server-side computed field evaluation against a running SurrealDB instance.
Requires the test container to be running (docker compose up).
"""

import pytest
from typing import Any, AsyncGenerator

from src.surreal_orm import SurrealDBConnectionManager
from src.surreal_orm.fields.computed import Computed
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict


# ---------------------------------------------------------------------------
# Connection config
# ---------------------------------------------------------------------------

SURREALDB_URL = "http://localhost:8001"
SURREALDB_USER = "root"
SURREALDB_PASS = "root"
SURREALDB_NAMESPACE = "test"
SURREALDB_DATABASE = "test_computed"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class PersonComputed(BaseSurrealModel):
    """Person model with computed full_name."""

    model_config = SurrealConfigDict(table_name="person_computed")
    id: str | None = None
    first_name: str
    last_name: str
    full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")


class OrderComputed(BaseSurrealModel):
    """Order model with multiple computed fields."""

    model_config = SurrealConfigDict(table_name="order_computed")
    id: str | None = None
    price: float = 0.0
    quantity: int = 1
    discount: float = 0.0
    subtotal: Computed[float] = Computed("price * quantity")
    total: Computed[float] = Computed("(price * quantity) * (1 - discount)")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[Any, Any]:
    """Setup connection and schema for the test module."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await SurrealDBConnectionManager.get_client()

    # Define schemas with VALUE clauses for computed fields
    await client.query("""
        REMOVE TABLE IF EXISTS person_computed;
        DEFINE TABLE person_computed SCHEMAFULL;
        DEFINE FIELD first_name ON person_computed TYPE string;
        DEFINE FIELD last_name ON person_computed TYPE string;
        DEFINE FIELD full_name ON person_computed TYPE option<string> VALUE string::concat(first_name, ' ', last_name);
    """)
    await client.query("""
        REMOVE TABLE IF EXISTS order_computed;
        DEFINE TABLE order_computed SCHEMAFULL;
        DEFINE FIELD price ON order_computed TYPE float;
        DEFINE FIELD quantity ON order_computed TYPE int;
        DEFINE FIELD discount ON order_computed TYPE float;
        DEFINE FIELD subtotal ON order_computed TYPE option<float> VALUE price * quantity;
        DEFINE FIELD total ON order_computed TYPE option<float> VALUE (price * quantity) * (1 - discount);
    """)
    yield
    await SurrealDBConnectionManager.close_connection()
    await SurrealDBConnectionManager.unset_connection()


@pytest.fixture(autouse=True)
async def cleanup_tables() -> AsyncGenerator[Any, Any]:
    """Clean up tables before and after each test."""
    client = await SurrealDBConnectionManager.get_client()
    try:
        await client.query("DELETE person_computed;")
        await client.query("DELETE order_computed;")
    except Exception:
        pass
    yield
    try:
        await client.query("DELETE person_computed;")
        await client.query("DELETE order_computed;")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestComputedFieldCreate:
    """Test computed fields on record creation."""

    async def test_full_name_computed_on_create(self) -> None:
        """SurrealDB should compute full_name from first_name + last_name."""
        person = PersonComputed(first_name="Alice", last_name="Smith")
        assert person.full_name is None  # Not yet computed

        await person.save()

        # Refresh from DB to get computed value
        await person.refresh()
        assert person.full_name == "Alice Smith"

    async def test_subtotal_and_total_computed(self) -> None:
        """SurrealDB should compute subtotal and total from price/quantity/discount."""
        order = OrderComputed(price=10.0, quantity=3, discount=0.1)
        await order.save()
        await order.refresh()

        assert order.subtotal == pytest.approx(30.0)
        assert order.total == pytest.approx(27.0)  # 30 * 0.9

    async def test_computed_field_not_in_save_data(self) -> None:
        """Computed fields should NOT be sent in the save data."""
        person = PersonComputed(first_name="Bob", last_name="Jones")
        exclude_fields = {"id"} | PersonComputed.get_server_fields()
        data = person.model_dump(exclude=exclude_fields, exclude_unset=True)

        assert "full_name" not in data
        assert "first_name" in data
        assert "last_name" in data


class TestComputedFieldUpdate:
    """Test computed fields update when source fields change."""

    async def test_full_name_updates_on_merge(self) -> None:
        """Computed field should update when source fields change."""
        person = PersonComputed(id="update_test", first_name="Alice", last_name="Smith")
        await person.save()
        await person.refresh()
        assert person.full_name == "Alice Smith"

        # Update first_name
        await person.merge(first_name="Bob")
        await person.refresh()
        assert person.full_name == "Bob Smith"


class TestComputedFieldQuery:
    """Test querying records with computed fields."""

    async def test_query_returns_computed_value(self) -> None:
        """Queried records should include computed field values."""
        person = PersonComputed(id="query_test", first_name="Charlie", last_name="Brown")
        await person.save()

        result = await PersonComputed.objects().get("query_test")
        assert result is not None
        assert result.full_name == "Charlie Brown"

    async def test_zero_discount_total(self) -> None:
        """Computed total with zero discount should equal subtotal."""
        order = OrderComputed(id="zero_discount", price=5.0, quantity=4, discount=0.0)
        await order.save()

        result = await OrderComputed.objects().get("zero_discount")
        assert result is not None
        assert result.subtotal == pytest.approx(20.0)
        assert result.total == pytest.approx(20.0)
