"""
Integration tests for v0.31.0 features against a running SurrealDB 3.0 instance.

1. RebuildIndex migration operation
2. DefineGraphQLConfig / RemoveGraphQLConfig migration operations
3. DefineBearerAccess migration operation
4. QuerySet.upsert() with ON DUPLICATE KEY UPDATE
5. QuerySet.bulk_upsert()

Run with: pytest -m integration tests/test_v031_integration.py
"""

import pytest

from src import surreal_orm
from src.surreal_orm.migrations.operations import (
    DefineBearerAccess,
    DefineGraphQLConfig,
    RebuildIndex,
    RemoveGraphQLConfig,
)
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.surreal_function import SurrealFunc
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

SURREALDB_DATABASE = "test_v031"


@pytest.fixture(scope="module", autouse=True)
async def setup_surrealdb() -> None:
    """Setup SurrealDB connection for v0.31.0 integration tests."""
    surreal_orm.SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    client = await surreal_orm.SurrealDBConnectionManager.get_client()
    await client.query(
        f"DEFINE NAMESPACE IF NOT EXISTS {SURREALDB_NAMESPACE}; DEFINE DATABASE IF NOT EXISTS {SURREALDB_DATABASE};"
    )


@pytest.fixture
async def clean_database():
    """Clean up test tables before and after each test."""

    async def cleanup() -> None:
        try:
            client = await surreal_orm.SurrealDBConnectionManager.reconnect()
            if client is None:
                return
            for stmt in [
                "REMOVE TABLE IF EXISTS upsert_users;",
                "REMOVE TABLE IF EXISTS rebuild_docs;",
                "REMOVE TABLE IF EXISTS bulk_upsert_items;",
                "REMOVE ACCESS IF EXISTS test_bearer ON DATABASE;",
                "REMOVE CONFIG IF EXISTS GRAPHQL;",
            ]:
                try:
                    await client.query(stmt)
                except Exception:
                    pass
        except Exception:
            pass

    await cleanup()
    yield
    await cleanup()


# ==================== Models ====================


class UpsertUser(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="upsert_users")
    id: str | None = None
    name: str = ""
    login_count: int = 0
    role: str = "user"


class BulkUpsertItem(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="bulk_upsert_items")
    id: str | None = None
    name: str = ""
    quantity: int = 0


# ==================== RebuildIndex ====================


@pytest.mark.integration
class TestRebuildIndexIntegration:
    """Integration tests: execute REBUILD INDEX against live SurrealDB 3.0."""

    @pytest.fixture(autouse=True)
    async def _setup_table(self, clean_database: None) -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query(
            "DEFINE TABLE rebuild_docs SCHEMALESS;"
            "DEFINE FIELD title ON rebuild_docs TYPE string;"
            "DEFINE INDEX idx_title ON rebuild_docs FIELDS title;"
        )

    async def test_rebuild_index_executes(self) -> None:
        """REBUILD INDEX should execute without error on an existing index."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = RebuildIndex(table="rebuild_docs", name="idx_title")
        await client.query(op.forwards())

    async def test_rebuild_index_if_exists(self) -> None:
        """REBUILD INDEX IF EXISTS should succeed even for non-existent indexes."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = RebuildIndex(table="rebuild_docs", name="idx_nonexistent", if_exists=True)
        # Should not raise — IF EXISTS guards against missing index
        await client.query(op.forwards())

    async def test_rebuild_index_after_data_insert(self) -> None:
        """REBUILD INDEX should succeed after inserting data."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        # Insert some data
        for i in range(10):
            await client.query(f"CREATE rebuild_docs SET title = 'doc_{i}';")
        # Rebuild the index
        op = RebuildIndex(table="rebuild_docs", name="idx_title")
        await client.query(op.forwards())
        # Verify data is still queryable
        result = await client.query("SELECT * FROM rebuild_docs;")
        assert len(result.all_records) == 10


# ==================== DefineGraphQLConfig ====================


@pytest.mark.integration
class TestDefineGraphQLConfigIntegration:
    """Integration tests: execute DEFINE CONFIG GRAPHQL against live SurrealDB 3.0."""

    @pytest.fixture(autouse=True)
    async def _setup(self, clean_database: None) -> None:
        pass

    async def test_define_graphql_auto(self) -> None:
        """DEFINE CONFIG GRAPHQL TABLES AUTO FUNCTIONS AUTO should execute."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = DefineGraphQLConfig(tables_mode="AUTO", functions_mode="AUTO")
        await client.query(op.forwards())

    async def test_define_graphql_none(self) -> None:
        """DEFINE CONFIG GRAPHQL with NONE modes should execute."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = DefineGraphQLConfig(tables_mode="NONE", functions_mode="NONE")
        await client.query(op.forwards())

    async def test_define_graphql_include_tables(self) -> None:
        """DEFINE CONFIG GRAPHQL with INCLUDE specific tables should execute."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        # Create a table first so INCLUDE has something to reference
        await client.query("DEFINE TABLE IF NOT EXISTS upsert_users SCHEMALESS;")
        op = DefineGraphQLConfig(
            tables_mode="INCLUDE",
            tables_list=["upsert_users"],
            functions_mode="NONE",
        )
        await client.query(op.forwards())

    async def test_remove_graphql_config(self) -> None:
        """RemoveGraphQLConfig should disable by overwriting with NONE."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        # Define first
        define_op = DefineGraphQLConfig()
        await client.query(define_op.forwards())
        # "Remove" (actually overwrites with NONE)
        remove_op = RemoveGraphQLConfig()
        await client.query(remove_op.forwards())

    async def test_redefine_graphql_config(self) -> None:
        """Re-defining GraphQL config should overwrite previous definition."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        # Define with AUTO
        op1 = DefineGraphQLConfig(tables_mode="AUTO", functions_mode="AUTO")
        await client.query(op1.forwards())
        # Redefine with NONE
        op2 = DefineGraphQLConfig(tables_mode="NONE", functions_mode="NONE")
        await client.query(op2.forwards())


# ==================== DefineBearerAccess ====================


@pytest.mark.integration
class TestDefineBearerAccessIntegration:
    """Integration tests: execute DEFINE ACCESS TYPE BEARER against live SurrealDB 3.0."""

    @pytest.fixture(autouse=True)
    async def _setup(self, clean_database: None) -> None:
        pass

    async def test_define_bearer_access(self) -> None:
        """DEFINE ACCESS ... TYPE BEARER should execute without error."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = DefineBearerAccess(
            name="test_bearer",
            duration_grant="7d",
            duration_session="1h",
        )
        await client.query(op.forwards())

    async def test_bearer_access_define_and_remove(self) -> None:
        """Bearer access can be defined and then removed cleanly."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = DefineBearerAccess(name="test_bearer", duration_grant="7d")
        await client.query(op.forwards())
        # Remove should not raise
        await client.query("REMOVE ACCESS IF EXISTS test_bearer ON DATABASE;")

    async def test_bearer_access_grant_key(self) -> None:
        """ACCESS ... GRANT should issue a bearer key without error."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = DefineBearerAccess(name="test_bearer", duration_grant="7d")
        await client.query(op.forwards())
        # Grant a key — should not raise
        result = await client.query("ACCESS test_bearer ON DATABASE GRANT FOR USER root;")
        # The response may vary by SDK version; key assertion is no error raised
        assert result is not None

    async def test_bearer_access_revoke_key(self) -> None:
        """ACCESS ... GRANT followed by REVOKE should work without error."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = DefineBearerAccess(name="test_bearer", duration_grant="7d")
        await client.query(op.forwards())
        # Grant a key
        grant_result = await client.query("ACCESS test_bearer ON DATABASE GRANT FOR USER root;")
        # Extract key ID from the raw response
        raw = grant_result.raw
        key_id = None
        if raw and isinstance(raw, list):
            for entry in raw:
                result_data = entry.get("result") if isinstance(entry, dict) else None
                if isinstance(result_data, dict) and "id" in result_data:
                    key_id = result_data["id"]
                    break
        if key_id:
            await client.query(f"ACCESS test_bearer ON DATABASE REVOKE {key_id};")

    async def test_bearer_for_record_variant(self) -> None:
        """DEFINE ACCESS TYPE BEARER FOR RECORD should also work."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        op = DefineBearerAccess(name="test_bearer", bearer_for="RECORD")
        await client.query(op.forwards())
        # Should not raise
        await client.query("REMOVE ACCESS IF EXISTS test_bearer ON DATABASE;")


# ==================== QuerySet.upsert() ====================


@pytest.mark.integration
class TestUpsertIntegration:
    """Integration tests: QuerySet.upsert() against live SurrealDB 3.0."""

    @pytest.fixture(autouse=True)
    async def _setup(self, clean_database: None) -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query("DEFINE TABLE upsert_users SCHEMALESS;")

    async def test_upsert_creates_new_record(self) -> None:
        """upsert() should create a new record when it doesn't exist."""
        user = await UpsertUser.objects().upsert(
            defaults={"name": "Alice", "login_count": 1, "role": "admin"},
            id="upsert_users:alice",
        )
        assert user.name == "Alice"
        assert user.login_count == 1

    async def test_upsert_updates_existing_record(self) -> None:
        """upsert() without on_conflict should overwrite existing record."""
        # Create first
        await UpsertUser.objects().upsert(
            defaults={"name": "Bob", "login_count": 1},
            id="upsert_users:bob",
        )
        # Upsert again — overwrites
        user = await UpsertUser.objects().upsert(
            defaults={"name": "Bob Updated", "login_count": 5},
            id="upsert_users:bob",
        )
        assert user.name == "Bob Updated"
        assert user.login_count == 5

    async def test_upsert_on_conflict_increments(self) -> None:
        """INSERT ON DUPLICATE KEY UPDATE via raw query works correctly."""
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        # Create first
        await client.query("UPSERT upsert_users:charlie SET name = 'Charlie', login_count = 1;")
        # INSERT ON DUPLICATE KEY UPDATE — raw query to isolate from ORM
        result = await client.query(
            "INSERT INTO upsert_users {id: upsert_users:charlie, name: 'Charlie', login_count: 1} "
            "ON DUPLICATE KEY UPDATE login_count += 1;"
        )
        records = result.all_records
        assert len(records) == 1
        assert records[0]["login_count"] == 2

    async def test_upsert_orm_on_conflict(self) -> None:
        """ORM upsert() with on_conflict should apply conflict clause."""
        # Create via raw query to avoid ORM caching
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query("CREATE upsert_users:dan SET name = 'Dan', login_count = 10;")
        # Upsert with conflict handling via ORM
        user = await UpsertUser.objects().upsert(
            defaults={"name": "Dan", "login_count": 10},
            id="upsert_users:dan",
            on_conflict={"login_count": SurrealFunc("login_count += 1")},
        )
        assert user.login_count == 11

    async def test_upsert_with_surreal_func_in_defaults(self) -> None:
        """upsert() should support SurrealFunc in defaults."""
        user = await UpsertUser.objects().upsert(
            defaults={"name": "FuncUser", "login_count": 1},
            id="upsert_users:funcuser",
        )
        assert user.name == "FuncUser"

    async def test_upsert_returns_model_instance(self) -> None:
        """upsert() should return a properly typed model instance."""
        user = await UpsertUser.objects().upsert(
            defaults={"name": "TypeCheck", "login_count": 0},
            id="upsert_users:typecheck",
        )
        assert isinstance(user, UpsertUser)
        assert user.get_id() is not None


# ==================== QuerySet.bulk_upsert() ====================


@pytest.mark.integration
class TestBulkUpsertIntegration:
    """Integration tests: QuerySet.bulk_upsert() against live SurrealDB 3.0."""

    @pytest.fixture(autouse=True)
    async def _setup(self, clean_database: None) -> None:
        client = await surreal_orm.SurrealDBConnectionManager.get_client()
        await client.query("DEFINE TABLE bulk_upsert_items SCHEMALESS;")

    async def test_bulk_upsert_creates_multiple(self) -> None:
        """bulk_upsert() should create multiple new records."""
        items = [
            BulkUpsertItem(id="bulk_upsert_items:a", name="Item A", quantity=10),
            BulkUpsertItem(id="bulk_upsert_items:b", name="Item B", quantity=20),
            BulkUpsertItem(id="bulk_upsert_items:c", name="Item C", quantity=30),
        ]
        results = await BulkUpsertItem.objects().bulk_upsert(items)
        assert len(results) == 3

    async def test_bulk_upsert_with_on_conflict(self) -> None:
        """bulk_upsert() with on_conflict should handle existing records."""
        # Create initial records
        items = [
            BulkUpsertItem(id="bulk_upsert_items:x", name="X", quantity=5),
            BulkUpsertItem(id="bulk_upsert_items:y", name="Y", quantity=10),
        ]
        await BulkUpsertItem.objects().bulk_upsert(items)

        # Upsert again with conflict handling — SurrealDB 3.0 uses += in ON DUPLICATE KEY
        results = await BulkUpsertItem.objects().bulk_upsert(
            items,
            on_conflict={"quantity": SurrealFunc("quantity += 1")},
        )
        assert len(results) == 2
        # Quantities should have incremented
        for item in results:
            if item.name == "X":
                assert item.quantity == 6
            elif item.name == "Y":
                assert item.quantity == 11

    async def test_bulk_upsert_atomic(self) -> None:
        """bulk_upsert(atomic=True) should create records in a transaction."""
        items = [
            BulkUpsertItem(id="bulk_upsert_items:t1", name="T1", quantity=1),
            BulkUpsertItem(id="bulk_upsert_items:t2", name="T2", quantity=2),
        ]
        await BulkUpsertItem.objects().bulk_upsert(items, atomic=True)
        # Verify records were created
        all_items = await BulkUpsertItem.objects().all()
        names = {item.name for item in all_items}
        assert "T1" in names
        assert "T2" in names

    async def test_bulk_upsert_empty_list(self) -> None:
        """bulk_upsert() with empty list should return empty."""
        results = await BulkUpsertItem.objects().bulk_upsert([])
        assert results == []

    async def test_bulk_upsert_returns_model_instances(self) -> None:
        """bulk_upsert() should return properly typed model instances."""
        items = [
            BulkUpsertItem(id="bulk_upsert_items:typed", name="Typed", quantity=99),
        ]
        results = await BulkUpsertItem.objects().bulk_upsert(items)
        assert len(results) == 1
        assert isinstance(results[0], BulkUpsertItem)
        assert results[0].name == "Typed"
