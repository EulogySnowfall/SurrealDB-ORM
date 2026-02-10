"""
Unit tests for multi-database connection registry.

Tests the named connection registry, ``using()`` context manager,
``ConnectionConfig``, and backward compatibility with the legacy API.
"""

from __future__ import annotations

import asyncio

import pytest

from surreal_orm.connection_config import ConnectionConfig
from surreal_orm.connection_manager import SurrealDBConnectionManager


# ---------------------------------------------------------------------------
# Fixture: clean registry before each test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure a clean registry before and after each test."""
    # Save existing state
    saved_configs = dict(SurrealDBConnectionManager._configs)
    saved_clients = dict(SurrealDBConnectionManager._clients)
    saved_ws_clients = dict(SurrealDBConnectionManager._ws_clients)

    # Clear
    SurrealDBConnectionManager._configs.clear()
    SurrealDBConnectionManager._clients.clear()
    SurrealDBConnectionManager._ws_clients.clear()
    SurrealDBConnectionManager._clear_legacy_vars()

    yield

    # Restore
    SurrealDBConnectionManager._configs.clear()
    SurrealDBConnectionManager._clients.clear()
    SurrealDBConnectionManager._ws_clients.clear()
    SurrealDBConnectionManager._clear_legacy_vars()

    SurrealDBConnectionManager._configs.update(saved_configs)
    SurrealDBConnectionManager._clients.update(saved_clients)
    SurrealDBConnectionManager._ws_clients.update(saved_ws_clients)
    # Re-sync legacy vars if default was in saved configs
    if "default" in saved_configs:
        SurrealDBConnectionManager._sync_legacy_vars(saved_configs["default"])


# ===========================================================================
# ConnectionConfig
# ===========================================================================


class TestConnectionConfig:
    def test_create_config(self):
        config = ConnectionConfig(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="test",
            database="test",
        )
        assert config.url == "http://localhost:8000"
        assert config.user == "root"
        assert config.protocol == "cbor"  # default

    def test_config_immutable(self):
        config = ConnectionConfig(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="test",
            database="test",
        )
        with pytest.raises(AttributeError):
            config.url = "http://other:8000"  # type: ignore[misc]

    def test_config_json_protocol(self):
        config = ConnectionConfig(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="test",
            database="test",
            protocol="json",
        )
        assert config.protocol == "json"


# ===========================================================================
# add_connection / remove_connection / list_connections / get_config
# ===========================================================================


class TestConnectionRegistry:
    def test_add_connection(self):
        SurrealDBConnectionManager.add_connection(
            "analytics",
            url="http://analytics:8000",
            user="root",
            password="root",
            namespace="analytics_ns",
            database="analytics_db",
        )
        assert "analytics" in SurrealDBConnectionManager.list_connections()
        config = SurrealDBConnectionManager.get_config("analytics")
        assert config is not None
        assert config.url == "http://analytics:8000"
        assert config.namespace == "analytics_ns"

    def test_add_default_syncs_legacy(self):
        SurrealDBConnectionManager.add_connection(
            "default",
            url="http://localhost:8000",
            user="root",
            password="secret",
            namespace="ns",
            database="db",
        )
        # Legacy getters should be updated
        assert SurrealDBConnectionManager.get_url() == "http://localhost:8000"
        assert SurrealDBConnectionManager.get_user() == "root"
        assert SurrealDBConnectionManager.get_namespace() == "ns"
        assert SurrealDBConnectionManager.get_database() == "db"
        assert SurrealDBConnectionManager.is_connection_set() is True

    def test_remove_connection(self):
        SurrealDBConnectionManager.add_connection(
            "temp",
            url="http://temp:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        assert "temp" in SurrealDBConnectionManager.list_connections()
        SurrealDBConnectionManager.remove_connection("temp")
        assert "temp" not in SurrealDBConnectionManager.list_connections()
        assert SurrealDBConnectionManager.get_config("temp") is None

    def test_remove_default_clears_legacy(self):
        SurrealDBConnectionManager.add_connection(
            "default",
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        SurrealDBConnectionManager.remove_connection("default")
        assert SurrealDBConnectionManager.get_url() is None
        assert SurrealDBConnectionManager.is_connection_set() is False

    def test_get_config_nonexistent(self):
        assert SurrealDBConnectionManager.get_config("nonexistent") is None

    def test_list_connections_empty(self):
        assert SurrealDBConnectionManager.list_connections() == []

    def test_list_connections_multiple(self):
        for name in ["default", "analytics", "reporting"]:
            SurrealDBConnectionManager.add_connection(
                name,
                url=f"http://{name}:8000",
                user="root",
                password="root",
                namespace="ns",
                database="db",
            )
        names = SurrealDBConnectionManager.list_connections()
        assert set(names) == {"default", "analytics", "reporting"}


# ===========================================================================
# set_connection backward compatibility
# ===========================================================================


class TestSetConnectionCompat:
    def test_set_connection_creates_default(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        assert SurrealDBConnectionManager.is_connection_set() is True
        config = SurrealDBConnectionManager.get_config("default")
        assert config is not None
        assert config.url == "http://localhost:8000"

    def test_set_connection_username_alias(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="ignored",
            password="root",
            namespace="ns",
            database="db",
            username="admin",
        )
        config = SurrealDBConnectionManager.get_config("default")
        assert config is not None
        assert config.user == "admin"

    def test_set_connection_protocol(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
            protocol="json",
        )
        assert SurrealDBConnectionManager.get_protocol() == "json"


# ===========================================================================
# unset_connection
# ===========================================================================


class TestUnsetConnection:
    @pytest.mark.asyncio
    async def test_unset_connection(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        assert SurrealDBConnectionManager.is_connection_set() is True
        await SurrealDBConnectionManager.unset_connection()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert SurrealDBConnectionManager.get_url() is None

    def test_unset_connection_sync(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        SurrealDBConnectionManager.unset_connection_sync()
        assert SurrealDBConnectionManager.is_connection_set() is False
        assert SurrealDBConnectionManager.get_url() is None


# ===========================================================================
# get_active_connection_name / using()
# ===========================================================================


class TestActiveConnection:
    def test_default_active_connection(self):
        assert SurrealDBConnectionManager.get_active_connection_name() == "default"

    @pytest.mark.asyncio
    async def test_using_overrides_active(self):
        SurrealDBConnectionManager.add_connection(
            "analytics",
            url="http://analytics:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        assert SurrealDBConnectionManager.get_active_connection_name() == "default"

        async with SurrealDBConnectionManager.using("analytics"):
            assert SurrealDBConnectionManager.get_active_connection_name() == "analytics"

        # After context exit, back to default
        assert SurrealDBConnectionManager.get_active_connection_name() == "default"

    @pytest.mark.asyncio
    async def test_using_unknown_connection_raises(self):
        with pytest.raises(ValueError, match="Unknown connection"):
            async with SurrealDBConnectionManager.using("nonexistent"):
                pass

    @pytest.mark.asyncio
    async def test_using_nested(self):
        """Nested using() should work correctly with contextvars."""
        SurrealDBConnectionManager.add_connection(
            "a",
            url="http://a:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        SurrealDBConnectionManager.add_connection(
            "b",
            url="http://b:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )

        assert SurrealDBConnectionManager.get_active_connection_name() == "default"

        async with SurrealDBConnectionManager.using("a"):
            assert SurrealDBConnectionManager.get_active_connection_name() == "a"
            async with SurrealDBConnectionManager.using("b"):
                assert SurrealDBConnectionManager.get_active_connection_name() == "b"
            assert SurrealDBConnectionManager.get_active_connection_name() == "a"

        assert SurrealDBConnectionManager.get_active_connection_name() == "default"

    @pytest.mark.asyncio
    async def test_using_async_safe(self):
        """using() should be isolated between concurrent tasks."""
        SurrealDBConnectionManager.add_connection(
            "conn_a",
            url="http://a:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        SurrealDBConnectionManager.add_connection(
            "conn_b",
            url="http://b:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )

        results: dict[str, str] = {}

        async def task_a():
            async with SurrealDBConnectionManager.using("conn_a"):
                await asyncio.sleep(0.01)
                results["a"] = SurrealDBConnectionManager.get_active_connection_name()

        async def task_b():
            async with SurrealDBConnectionManager.using("conn_b"):
                await asyncio.sleep(0.01)
                results["b"] = SurrealDBConnectionManager.get_active_connection_name()

        await asyncio.gather(task_a(), task_b())

        assert results["a"] == "conn_a"
        assert results["b"] == "conn_b"


# ===========================================================================
# get_client / get_ws_client error paths
# ===========================================================================


class TestGetClientErrors:
    @pytest.mark.asyncio
    async def test_get_client_unconfigured_raises(self):
        with pytest.raises(ValueError, match="not configured"):
            await SurrealDBConnectionManager.get_client()

    @pytest.mark.asyncio
    async def test_get_client_named_unconfigured_raises(self):
        with pytest.raises(ValueError, match="not configured"):
            await SurrealDBConnectionManager.get_client("nonexistent")

    @pytest.mark.asyncio
    async def test_get_ws_client_unconfigured_raises(self):
        with pytest.raises(ValueError, match="not configured"):
            await SurrealDBConnectionManager.get_ws_client()

    @pytest.mark.asyncio
    async def test_get_ws_client_named_unconfigured_raises(self):
        with pytest.raises(ValueError, match="not configured"):
            await SurrealDBConnectionManager.get_ws_client("nonexistent")


# ===========================================================================
# Legacy individual setters
# ===========================================================================


class TestLegacySetters:
    @pytest.mark.asyncio
    async def test_set_url_no_connection_raises(self):
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.set_url("http://new:8000")

    @pytest.mark.asyncio
    async def test_set_user_no_connection_raises(self):
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.set_user("new_user")

    @pytest.mark.asyncio
    async def test_set_password_no_connection_raises(self):
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.set_password("new_pass")

    @pytest.mark.asyncio
    async def test_set_namespace_no_connection_raises(self):
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.set_namespace("new_ns")

    @pytest.mark.asyncio
    async def test_set_database_no_connection_raises(self):
        with pytest.raises(ValueError):
            await SurrealDBConnectionManager.set_database("new_db")

    @pytest.mark.asyncio
    async def test_set_url_updates_config(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        result = await SurrealDBConnectionManager.set_url("http://newhost:8000")
        assert result is True
        assert SurrealDBConnectionManager.get_url() == "http://newhost:8000"
        config = SurrealDBConnectionManager.get_config("default")
        assert config is not None
        assert config.url == "http://newhost:8000"

    @pytest.mark.asyncio
    async def test_set_user_updates_config(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        result = await SurrealDBConnectionManager.set_user("admin")
        assert result is True
        assert SurrealDBConnectionManager.get_user() == "admin"

    @pytest.mark.asyncio
    async def test_set_password_updates_config(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        result = await SurrealDBConnectionManager.set_password("secret")
        assert result is True
        assert SurrealDBConnectionManager.is_password_set() is True

    @pytest.mark.asyncio
    async def test_set_namespace_updates_config(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        result = await SurrealDBConnectionManager.set_namespace("new_ns")
        assert result is True
        assert SurrealDBConnectionManager.get_namespace() == "new_ns"

    @pytest.mark.asyncio
    async def test_set_database_updates_config(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        result = await SurrealDBConnectionManager.set_database("new_db")
        assert result is True
        assert SurrealDBConnectionManager.get_database() == "new_db"


# ===========================================================================
# is_connected / is_password_set
# ===========================================================================


class TestStatusChecks:
    def test_is_connected_false_by_default(self):
        assert SurrealDBConnectionManager.is_connected() is False

    def test_is_password_set_false_by_default(self):
        assert SurrealDBConnectionManager.is_password_set() is False

    def test_is_password_set_true(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="secret",
            namespace="ns",
            database="db",
        )
        assert SurrealDBConnectionManager.is_password_set() is True

    def test_get_connection_kwargs(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        kwargs = SurrealDBConnectionManager.get_connection_kwargs()
        assert kwargs["url"] == "http://localhost:8000"
        assert kwargs["user"] == "root"
        assert kwargs["namespace"] == "ns"
        assert kwargs["database"] == "db"

    def test_get_connection_string(self):
        SurrealDBConnectionManager.set_connection(
            url="http://localhost:8000",
            user="root",
            password="root",
            namespace="ns",
            database="db",
        )
        assert SurrealDBConnectionManager.get_connection_string() == "http://localhost:8000"
