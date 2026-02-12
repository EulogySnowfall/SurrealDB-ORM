"""Tests for Model Signals."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

import pytest
from pydantic import Field

from surreal_orm import (
    # Around signals
    AroundSignal,
    BaseSurrealModel,
    Signal,
    SurrealConfigDict,
    SurrealDBConnectionManager,
    around_delete,
    around_save,
    around_update,
    post_delete,
    post_save,
    post_update,
    pre_delete,
    pre_save,
    pre_update,
)
from tests.conftest import SURREALDB_NAMESPACE, SURREALDB_PASS, SURREALDB_URL, SURREALDB_USER

# Each test file uses its own database for isolation
SURREALDB_DATABASE = "test_signals"


# =============================================================================
# Test Models
# =============================================================================


class SignalTestModel(BaseSurrealModel):
    """Test model for signal testing."""

    model_config = SurrealConfigDict(
        table_name="signal_test_models",
    )

    id: str | None = None
    name: str = Field(default="test")
    value: int = Field(default=0)


# =============================================================================
# Unit Tests - Signal Class
# =============================================================================


class TestSignalClass:
    """Unit tests for the Signal class itself."""

    def test_signal_creation(self) -> None:
        """Test that a signal can be created."""
        signal = Signal("test_signal")
        assert signal.name == "test_signal"
        assert signal._handlers == {}

    def test_connect_with_sender(self) -> None:
        """Test connecting a handler with a specific sender."""
        signal = Signal("test")
        received: list[dict[str, Any]] = []

        @signal.connect(SignalTestModel)
        async def handler(sender: type, instance: Any, **kwargs: Any) -> None:
            received.append({"sender": sender, "instance": instance})

        assert SignalTestModel in signal._handlers
        assert len(signal._handlers[SignalTestModel]) == 1

    def test_connect_without_sender(self) -> None:
        """Test connecting a handler for all senders."""
        signal = Signal("test")

        @signal.connect()
        async def handler(sender: type, **kwargs: Any) -> None:
            pass

        assert None in signal._handlers
        assert len(signal._handlers[None]) == 1

    def test_connect_without_parentheses(self) -> None:
        """Test connecting a handler without parentheses."""
        signal = Signal("test")

        @signal.connect
        async def handler(sender: type, **kwargs: Any) -> None:
            pass

        assert None in signal._handlers
        assert len(signal._handlers[None]) == 1

    def test_disconnect(self) -> None:
        """Test disconnecting a handler."""
        signal = Signal("test")

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> None:
            pass

        assert len(signal._handlers[SignalTestModel]) == 1

        result = signal.disconnect(handler, sender=SignalTestModel)
        assert result is True
        assert len(signal._handlers[SignalTestModel]) == 0

    def test_disconnect_not_found(self) -> None:
        """Test disconnecting a handler that wasn't connected."""
        signal = Signal("test")

        async def handler(sender: type, **kwargs: Any) -> None:
            pass

        result = signal.disconnect(handler, sender=SignalTestModel)
        assert result is False

    def test_disconnect_all(self) -> None:
        """Test disconnecting all handlers for a sender."""
        signal = Signal("test")

        @signal.connect(SignalTestModel)
        async def handler1(sender: type, **kwargs: Any) -> None:
            pass

        @signal.connect(SignalTestModel)
        async def handler2(sender: type, **kwargs: Any) -> None:
            pass

        assert len(signal._handlers[SignalTestModel]) == 2

        count = signal.disconnect_all(sender=SignalTestModel)
        assert count == 2
        assert len(signal._handlers[SignalTestModel]) == 0

    def test_clear(self) -> None:
        """Test clearing all handlers."""
        signal = Signal("test")

        @signal.connect(SignalTestModel)
        async def handler1(sender: type, **kwargs: Any) -> None:
            pass

        @signal.connect()
        async def handler2(sender: type, **kwargs: Any) -> None:
            pass

        signal.clear()
        assert signal._handlers == {}

    @pytest.mark.asyncio
    async def test_send_to_specific_sender(self) -> None:
        """Test sending signal to handlers for a specific sender."""
        signal = Signal("test")
        received: list[str] = []

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> None:
            received.append("specific")

        await signal.send(SignalTestModel, instance=None)
        assert received == ["specific"]

    @pytest.mark.asyncio
    async def test_send_to_global_handlers(self) -> None:
        """Test sending signal to global handlers (no sender filter)."""
        signal = Signal("test")
        received: list[str] = []

        @signal.connect()
        async def handler(sender: type, **kwargs: Any) -> None:
            received.append("global")

        await signal.send(SignalTestModel, instance=None)
        assert received == ["global"]

    @pytest.mark.asyncio
    async def test_send_to_both_specific_and_global(self) -> None:
        """Test that both specific and global handlers receive the signal."""
        signal = Signal("test")
        received: list[str] = []

        @signal.connect(SignalTestModel)
        async def specific_handler(sender: type, **kwargs: Any) -> None:
            received.append("specific")

        @signal.connect()
        async def global_handler(sender: type, **kwargs: Any) -> None:
            received.append("global")

        await signal.send(SignalTestModel, instance=None)
        assert "specific" in received
        assert "global" in received
        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_send_doesnt_call_other_senders(self) -> None:
        """Test that handlers for other senders are not called."""
        signal = Signal("test")
        received: list[str] = []

        class OtherModel(BaseSurrealModel):
            id: str | None = None

        @signal.connect(OtherModel)
        async def other_handler(sender: type, **kwargs: Any) -> None:
            received.append("other")

        await signal.send(SignalTestModel, instance=None)
        assert received == []

    @pytest.mark.asyncio
    async def test_handler_exception_doesnt_break_others(self) -> None:
        """Test that one handler exception doesn't prevent others from running."""
        signal = Signal("test")
        received: list[str] = []

        @signal.connect(SignalTestModel)
        async def bad_handler(sender: type, **kwargs: Any) -> None:
            raise ValueError("Intentional error")

        @signal.connect(SignalTestModel)
        async def good_handler(sender: type, **kwargs: Any) -> None:
            received.append("good")

        results = await signal.send(SignalTestModel, instance=None)

        # Good handler should still have run
        assert "good" in received

        # Results should include the exception
        assert len(results) == 2
        exceptions = [r[1] for r in results if isinstance(r[1], Exception)]
        assert len(exceptions) == 1
        assert isinstance(exceptions[0], ValueError)

    def test_has_receivers(self) -> None:
        """Test has_receivers method."""
        signal = Signal("test")

        assert signal.has_receivers() is False
        assert signal.has_receivers(SignalTestModel) is False

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> None:
            pass

        assert signal.has_receivers() is True
        assert signal.has_receivers(SignalTestModel) is True

    def test_receivers_property(self) -> None:
        """Test receivers property returns a copy."""
        signal = Signal("test")

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> None:
            pass

        receivers = signal.receivers
        assert SignalTestModel in receivers
        assert len(receivers[SignalTestModel]) == 1

        # Modify the copy - original should be unchanged
        receivers[SignalTestModel].clear()
        assert len(signal._handlers[SignalTestModel]) == 1

    def test_avoid_duplicate_handlers(self) -> None:
        """Test that the same handler isn't added twice."""
        signal = Signal("test")

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> None:
            pass

        # Try to connect the same handler again
        signal.connect(SignalTestModel)(handler)

        assert len(signal._handlers[SignalTestModel]) == 1


# =============================================================================
# Unit Tests - Pre-defined Signals
# =============================================================================


class TestPredefinedSignals:
    """Test that pre-defined signals are properly configured."""

    def test_pre_save_exists(self) -> None:
        """Test pre_save signal exists."""
        assert pre_save.name == "pre_save"

    def test_post_save_exists(self) -> None:
        """Test post_save signal exists."""
        assert post_save.name == "post_save"

    def test_pre_delete_exists(self) -> None:
        """Test pre_delete signal exists."""
        assert pre_delete.name == "pre_delete"

    def test_post_delete_exists(self) -> None:
        """Test post_delete signal exists."""
        assert post_delete.name == "post_delete"

    def test_pre_update_exists(self) -> None:
        """Test pre_update signal exists."""
        assert pre_update.name == "pre_update"

    def test_post_update_exists(self) -> None:
        """Test post_update signal exists."""
        assert post_update.name == "post_update"


# =============================================================================
# Integration Tests - Signal Arguments
# =============================================================================


class TestSignalArguments:
    """Test that signals receive correct arguments."""

    @pytest.mark.asyncio
    async def test_post_save_receives_created_true_for_new_record(self) -> None:
        """Test post_save receives created=True for new records."""
        received: list[dict[str, Any]] = []

        @post_save.connect(SignalTestModel)
        async def handler(
            sender: type,
            instance: SignalTestModel,
            created: bool,
            **kwargs: Any,
        ) -> None:
            received.append(
                {
                    "sender": sender,
                    "instance_name": instance.name,
                    "created": created,
                }
            )

        try:
            # Manually trigger the signal as we would in save()
            instance = SignalTestModel(name="test_new")
            await post_save.send(
                sender=SignalTestModel,
                instance=instance,
                created=True,
                tx=None,
            )

            assert len(received) == 1
            assert received[0]["sender"] == SignalTestModel
            assert received[0]["instance_name"] == "test_new"
            assert received[0]["created"] is True
        finally:
            post_save.disconnect(handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_post_save_receives_created_false_for_update(self) -> None:
        """Test post_save receives created=False for updates."""
        received: list[dict[str, Any]] = []

        @post_save.connect(SignalTestModel)
        async def handler(
            sender: type,
            instance: SignalTestModel,
            created: bool,
            **kwargs: Any,
        ) -> None:
            received.append({"created": created})

        try:
            instance = SignalTestModel(name="test_update")
            await post_save.send(
                sender=SignalTestModel,
                instance=instance,
                created=False,
                tx=None,
            )

            assert len(received) == 1
            assert received[0]["created"] is False
        finally:
            post_save.disconnect(handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_pre_delete_receives_instance(self) -> None:
        """Test pre_delete receives the instance being deleted."""
        received: list[dict[str, Any]] = []

        @pre_delete.connect(SignalTestModel)
        async def handler(
            sender: type,
            instance: SignalTestModel,
            **kwargs: Any,
        ) -> None:
            received.append({"instance_name": instance.name})

        try:
            instance = SignalTestModel(name="to_delete")
            await pre_delete.send(
                sender=SignalTestModel,
                instance=instance,
                tx=None,
            )

            assert len(received) == 1
            assert received[0]["instance_name"] == "to_delete"
        finally:
            pre_delete.disconnect(handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_post_update_receives_update_fields(self) -> None:
        """Test post_update receives the fields being updated."""
        received: list[dict[str, Any]] = []

        @post_update.connect(SignalTestModel)
        async def handler(
            sender: type,
            instance: SignalTestModel,
            update_fields: dict[str, Any],
            **kwargs: Any,
        ) -> None:
            received.append({"update_fields": update_fields})

        try:
            instance = SignalTestModel(name="test")
            await post_update.send(
                sender=SignalTestModel,
                instance=instance,
                update_fields={"name": "updated", "value": 42},
                tx=None,
            )

            assert len(received) == 1
            assert received[0]["update_fields"] == {"name": "updated", "value": 42}
        finally:
            post_update.disconnect(handler, sender=SignalTestModel)


# =============================================================================
# Unit Tests - AroundSignal Class (Generator-based middleware)
# =============================================================================


class TestAroundSignalClass:
    """Unit tests for the AroundSignal class itself."""

    def test_around_signal_creation(self) -> None:
        """Test that an around signal can be created."""
        signal = AroundSignal("test_around")
        assert signal.name == "test_around"
        assert signal._handlers == {}

    def test_connect_with_sender(self) -> None:
        """Test connecting a handler with a specific sender."""
        signal = AroundSignal("test")

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield

        assert SignalTestModel in signal._handlers
        assert handler in signal._handlers[SignalTestModel]

        # Cleanup
        signal.clear()

    def test_connect_without_sender(self) -> None:
        """Test connecting a handler without a sender (global handler)."""
        signal = AroundSignal("test")

        @signal.connect()
        async def handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield

        assert None in signal._handlers
        assert handler in signal._handlers[None]

        # Cleanup
        signal.clear()

    def test_connect_without_parentheses(self) -> None:
        """Test connecting a handler using decorator without parentheses."""
        signal = AroundSignal("test")

        @signal.connect
        async def handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield

        assert None in signal._handlers
        assert handler in signal._handlers[None]

        # Cleanup
        signal.clear()

    def test_disconnect(self) -> None:
        """Test disconnecting a handler."""
        signal = AroundSignal("test")

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield

        assert signal.disconnect(handler, sender=SignalTestModel)
        assert handler not in signal._handlers.get(SignalTestModel, [])

    def test_disconnect_nonexistent(self) -> None:
        """Test disconnecting a handler that doesn't exist."""
        signal = AroundSignal("test")

        async def handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield

        assert not signal.disconnect(handler, sender=SignalTestModel)

    def test_clear(self) -> None:
        """Test clearing all handlers."""
        signal = AroundSignal("test")

        @signal.connect(SignalTestModel)
        async def handler1(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield

        @signal.connect()
        async def handler2(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield

        signal.clear()
        assert signal._handlers == {}

    def test_has_receivers(self) -> None:
        """Test has_receivers method."""
        signal = AroundSignal("test")

        assert not signal.has_receivers()
        assert not signal.has_receivers(SignalTestModel)

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield

        assert signal.has_receivers()
        assert signal.has_receivers(SignalTestModel)

        # Cleanup
        signal.clear()

    @pytest.mark.asyncio
    async def test_wrap_executes_before_and_after(self) -> None:
        """Test that wrap() executes code before and after yield."""
        signal = AroundSignal("test")
        execution_order: list[str] = []

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            execution_order.append("before")
            yield
            execution_order.append("after")

        async with signal.wrap(SignalTestModel, instance=None):
            execution_order.append("operation")

        assert execution_order == ["before", "operation", "after"]

        # Cleanup
        signal.clear()

    @pytest.mark.asyncio
    async def test_wrap_with_multiple_handlers(self) -> None:
        """Test that wrap() executes multiple handlers in order."""
        signal = AroundSignal("test")
        execution_order: list[str] = []

        @signal.connect(SignalTestModel)
        async def handler1(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            execution_order.append("h1_before")
            yield
            execution_order.append("h1_after")

        @signal.connect(SignalTestModel)
        async def handler2(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            execution_order.append("h2_before")
            yield
            execution_order.append("h2_after")

        async with signal.wrap(SignalTestModel, instance=None):
            execution_order.append("operation")

        # After code runs in reverse order (LIFO for cleanup)
        assert execution_order == ["h1_before", "h2_before", "operation", "h2_after", "h1_after"]

        # Cleanup
        signal.clear()

    @pytest.mark.asyncio
    async def test_wrap_with_no_handlers(self) -> None:
        """Test that wrap() works with no handlers."""
        signal = AroundSignal("test")
        executed = False

        async with signal.wrap(SignalTestModel, instance=None):
            executed = True

        assert executed

    @pytest.mark.asyncio
    async def test_wrap_with_global_and_specific_handlers(self) -> None:
        """Test that wrap() executes both global and sender-specific handlers."""
        signal = AroundSignal("test")
        execution_order: list[str] = []

        @signal.connect(SignalTestModel)
        async def specific_handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            execution_order.append("specific_before")
            yield
            execution_order.append("specific_after")

        @signal.connect()
        async def global_handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            execution_order.append("global_before")
            yield
            execution_order.append("global_after")

        async with signal.wrap(SignalTestModel, instance=None):
            execution_order.append("operation")

        # Specific handlers first, then global
        assert "specific_before" in execution_order
        assert "global_before" in execution_order
        assert "operation" in execution_order
        assert "specific_after" in execution_order
        assert "global_after" in execution_order

        # Cleanup
        signal.clear()

    @pytest.mark.asyncio
    async def test_wrap_handler_exception_before_yield(self) -> None:
        """Test that exceptions in before code are logged but don't break wrap."""
        signal = AroundSignal("test")

        @signal.connect(SignalTestModel)
        async def bad_handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            raise ValueError("Error before yield")
            yield  # type: ignore  # unreachable

        executed = False
        async with signal.wrap(SignalTestModel, instance=None):
            executed = True

        assert executed  # Operation should still execute

        # Cleanup
        signal.clear()

    @pytest.mark.asyncio
    async def test_wrap_handler_exception_after_yield(self) -> None:
        """Test that exceptions in after code are logged but don't break wrap."""
        signal = AroundSignal("test")

        @signal.connect(SignalTestModel)
        async def bad_handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            yield
            raise ValueError("Error after yield")

        executed = False
        # Should not raise, exception is logged
        async with signal.wrap(SignalTestModel, instance=None):
            executed = True

        assert executed

        # Cleanup
        signal.clear()

    @pytest.mark.asyncio
    async def test_wrap_passes_kwargs_to_handlers(self) -> None:
        """Test that kwargs are passed to handlers."""
        signal = AroundSignal("test")
        received_kwargs: dict[str, Any] = {}

        @signal.connect(SignalTestModel)
        async def handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            received_kwargs.update(kwargs)
            yield

        async with signal.wrap(SignalTestModel, instance="test_instance", custom="value"):
            pass

        assert received_kwargs["instance"] == "test_instance"
        assert received_kwargs["custom"] == "value"

        # Cleanup
        signal.clear()

    @pytest.mark.asyncio
    async def test_wrap_with_try_finally(self) -> None:
        """Test that handlers can use try/finally for guaranteed cleanup."""
        signal = AroundSignal("test")
        cleanup_called = False

        @signal.connect(SignalTestModel)
        async def handler_with_cleanup(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            nonlocal cleanup_called
            try:
                yield
            finally:
                cleanup_called = True

        # Even if wrapped code raises, cleanup should run
        try:
            async with signal.wrap(SignalTestModel, instance=None):
                raise RuntimeError("Operation failed")
        except RuntimeError:
            pass

        assert cleanup_called

        # Cleanup
        signal.clear()

    @pytest.mark.asyncio
    async def test_wrap_shared_state_between_before_and_after(self) -> None:
        """Test that handlers can share state between before and after."""
        signal = AroundSignal("test")
        timing: dict[str, float] = {}

        @signal.connect(SignalTestModel)
        async def timing_handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            import time

            start = time.time()
            yield
            timing["duration"] = time.time() - start

        async with signal.wrap(SignalTestModel, instance=None):
            import asyncio

            await asyncio.sleep(0.01)  # Small delay

        assert "duration" in timing
        assert timing["duration"] >= 0.01

        # Cleanup
        signal.clear()


class TestAroundSignalPreDefined:
    """Test pre-defined around signals."""

    def test_around_save_exists(self) -> None:
        """Test that around_save signal exists."""
        assert around_save.name == "around_save"

    def test_around_delete_exists(self) -> None:
        """Test that around_delete signal exists."""
        assert around_delete.name == "around_delete"

    def test_around_update_exists(self) -> None:
        """Test that around_update signal exists."""
        assert around_update.name == "around_update"


# =============================================================================
# Integration Tests - Real Database Operations
# =============================================================================


@pytest.fixture(scope="module", autouse=True)
async def setup_connection() -> AsyncGenerator[Any, Any]:
    """Setup SurrealDB connection for integration tests."""
    SurrealDBConnectionManager.set_connection(
        SURREALDB_URL,
        SURREALDB_USER,
        SURREALDB_PASS,
        SURREALDB_NAMESPACE,
        SURREALDB_DATABASE,
    )
    yield
    await SurrealDBConnectionManager.unset_connection()


@pytest.mark.integration
class TestSignalsWithDatabase:
    """Integration tests that verify signals fire during real database operations."""

    @pytest.mark.asyncio
    async def test_save_new_record_fires_signals(self) -> None:
        """Test that save() on a new record fires pre_save and post_save with created=True."""
        pre_save_received: list[dict[str, Any]] = []
        post_save_received: list[dict[str, Any]] = []

        @pre_save.connect(SignalTestModel)
        async def pre_handler(sender: type, instance: SignalTestModel, created: bool, **kwargs: Any) -> None:
            pre_save_received.append({"instance_name": instance.name, "created": created})

        @post_save.connect(SignalTestModel)
        async def post_handler(sender: type, instance: SignalTestModel, created: bool, **kwargs: Any) -> None:
            post_save_received.append({"instance_name": instance.name, "created": created, "id": instance.id})

        try:
            # Create and save a new record
            instance = SignalTestModel(id="signal_test_1", name="test_new", value=42)
            await instance.save()

            # Verify pre_save was called with created=True
            assert len(pre_save_received) == 1
            assert pre_save_received[0]["instance_name"] == "test_new"
            assert pre_save_received[0]["created"] is True

            # Verify post_save was called with created=True
            assert len(post_save_received) == 1
            assert post_save_received[0]["instance_name"] == "test_new"
            assert post_save_received[0]["created"] is True
            assert post_save_received[0]["id"] == "signal_test_1"

            # Cleanup
            await instance.delete()
        finally:
            pre_save.disconnect(pre_handler, sender=SignalTestModel)
            post_save.disconnect(post_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_save_existing_record_fires_signals_with_created_false(self) -> None:
        """Test that save() on an existing record fires signals with created=False."""
        post_save_received: list[dict[str, Any]] = []

        @post_save.connect(SignalTestModel)
        async def handler(sender: type, instance: SignalTestModel, created: bool, **kwargs: Any) -> None:
            post_save_received.append({"created": created, "value": instance.value})

        try:
            # Create a new record
            instance = SignalTestModel(id="signal_test_2", name="test", value=1)
            await instance.save()

            # Clear the list to only capture the update signal
            post_save_received.clear()

            # Update and save the existing record
            instance.value = 2
            await instance.save()

            # Verify post_save was called with created=False
            assert len(post_save_received) == 1
            assert post_save_received[0]["created"] is False
            assert post_save_received[0]["value"] == 2

            # Cleanup
            await instance.delete()
        finally:
            post_save.disconnect(handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_delete_fires_signals(self) -> None:
        """Test that delete() fires pre_delete and post_delete signals."""
        pre_delete_received: list[dict[str, Any]] = []
        post_delete_received: list[dict[str, Any]] = []

        @pre_delete.connect(SignalTestModel)
        async def pre_handler(sender: type, instance: SignalTestModel, **kwargs: Any) -> None:
            pre_delete_received.append({"instance_id": instance.id})

        @post_delete.connect(SignalTestModel)
        async def post_handler(sender: type, instance: SignalTestModel, **kwargs: Any) -> None:
            post_delete_received.append({"instance_id": instance.id})

        try:
            # Create a record
            instance = SignalTestModel(id="signal_test_3", name="to_delete")
            await instance.save()

            # Delete the record
            await instance.delete()

            # Verify pre_delete was called
            assert len(pre_delete_received) == 1
            assert pre_delete_received[0]["instance_id"] == "signal_test_3"

            # Verify post_delete was called
            assert len(post_delete_received) == 1
            assert post_delete_received[0]["instance_id"] == "signal_test_3"
        finally:
            pre_delete.disconnect(pre_handler, sender=SignalTestModel)
            post_delete.disconnect(post_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_update_fires_signals(self) -> None:
        """Test that update() fires pre_update and post_update signals."""
        pre_update_received: list[dict[str, Any]] = []
        post_update_received: list[dict[str, Any]] = []

        @pre_update.connect(SignalTestModel)
        async def pre_handler(
            sender: type,
            instance: SignalTestModel,
            update_fields: dict[str, Any],
            **kwargs: Any,
        ) -> None:
            pre_update_received.append({"update_fields": update_fields.copy()})

        @post_update.connect(SignalTestModel)
        async def post_handler(
            sender: type,
            instance: SignalTestModel,
            update_fields: dict[str, Any],
            **kwargs: Any,
        ) -> None:
            post_update_received.append({"update_fields": update_fields.copy()})

        try:
            # Create a record
            instance = SignalTestModel(id="signal_test_4", name="original", value=0)
            await instance.save()

            # Update the record
            instance.name = "updated"
            instance.value = 100
            await instance.update()

            # Verify pre_update was called with correct fields
            assert len(pre_update_received) == 1
            assert "name" in pre_update_received[0]["update_fields"]
            assert pre_update_received[0]["update_fields"]["name"] == "updated"

            # Verify post_update was called with correct fields
            assert len(post_update_received) == 1
            assert "name" in post_update_received[0]["update_fields"]

            # Cleanup
            await instance.delete()
        finally:
            pre_update.disconnect(pre_handler, sender=SignalTestModel)
            post_update.disconnect(post_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_merge_fires_signals(self) -> None:
        """Test that merge() fires pre_update and post_update signals."""
        post_update_received: list[dict[str, Any]] = []

        @post_update.connect(SignalTestModel)
        async def handler(
            sender: type,
            instance: SignalTestModel,
            update_fields: dict[str, Any],
            **kwargs: Any,
        ) -> None:
            post_update_received.append({"update_fields": update_fields.copy()})

        try:
            # Create a record
            instance = SignalTestModel(id="signal_test_5", name="original", value=0)
            await instance.save()

            # Merge partial update
            await instance.merge(name="merged_name")

            # Verify post_update was called with the merged fields
            assert len(post_update_received) == 1
            assert post_update_received[0]["update_fields"] == {"name": "merged_name"}

            # Cleanup
            await instance.delete()
        finally:
            post_update.disconnect(handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_signal_handler_exception_doesnt_break_save(self) -> None:
        """Test that a failing signal handler doesn't prevent save from completing."""

        @post_save.connect(SignalTestModel)
        async def bad_handler(sender: type, **kwargs: Any) -> None:
            raise ValueError("Intentional error in signal handler")

        try:
            # Create and save a record - should succeed despite handler error
            instance = SignalTestModel(id="signal_test_6", name="test", value=1)
            await instance.save()

            # Verify the record was saved by fetching it
            loaded = await SignalTestModel.objects().get("signal_test_6")
            assert loaded is not None
            assert loaded.name == "test"

            # Cleanup
            await instance.delete()
        finally:
            post_save.disconnect(bad_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_global_handler_receives_all_model_signals(self) -> None:
        """Test that a handler connected without sender receives signals from all models."""
        received: list[dict[str, Any]] = []

        @post_save.connect()
        async def global_handler(sender: type, instance: Any, created: bool, **kwargs: Any) -> None:
            received.append({"sender": sender.__name__, "created": created})

        try:
            # Create and save a record
            instance = SignalTestModel(id="signal_test_7", name="global_test")
            await instance.save()

            # Verify global handler was called
            assert len(received) >= 1
            assert any(r["sender"] == "SignalTestModel" and r["created"] is True for r in received)

            # Cleanup
            await instance.delete()
        finally:
            post_save.disconnect(global_handler, sender=None)


@pytest.mark.integration
class TestAroundSignalsWithDatabase:
    """Integration tests that verify around signals wrap real database operations."""

    @pytest.mark.asyncio
    async def test_around_save_wraps_save_operation(self) -> None:
        """Test that around_save wraps the save() operation correctly."""
        execution_order: list[str] = []

        @around_save.connect(SignalTestModel)
        async def timing_handler(
            sender: type, instance: SignalTestModel, created: bool, **kwargs: Any
        ) -> AsyncGenerator[None, None]:
            execution_order.append(f"before_save:created={created}")
            yield
            execution_order.append(f"after_save:id={instance.id}")

        try:
            instance = SignalTestModel(id="around_test_1", name="test", value=42)
            await instance.save()

            assert "before_save:created=True" in execution_order
            assert "after_save:id=around_test_1" in execution_order
            # Verify order
            assert execution_order.index("before_save:created=True") < execution_order.index("after_save:id=around_test_1")

            # Cleanup
            await instance.delete()
        finally:
            around_save.disconnect(timing_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_around_save_can_measure_timing(self) -> None:
        """Test that around_save can be used to measure operation timing."""
        import time

        timing: dict[str, float] = {}

        @around_save.connect(SignalTestModel)
        async def timing_handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            start = time.time()
            yield
            timing["duration"] = time.time() - start

        try:
            instance = SignalTestModel(id="around_test_2", name="timing_test")
            await instance.save()

            assert "duration" in timing
            assert timing["duration"] >= 0  # Should be non-negative

            # Cleanup
            await instance.delete()
        finally:
            around_save.disconnect(timing_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_around_delete_wraps_delete_operation(self) -> None:
        """Test that around_delete wraps the delete() operation correctly."""
        execution_order: list[str] = []

        @around_delete.connect(SignalTestModel)
        async def delete_handler(sender: type, instance: SignalTestModel, **kwargs: Any) -> AsyncGenerator[None, None]:
            execution_order.append(f"before_delete:id={instance.id}")
            yield
            execution_order.append("after_delete")

        try:
            # Create a record first
            instance = SignalTestModel(id="around_test_3", name="to_delete")
            await instance.save()

            # Delete it
            await instance.delete()

            assert "before_delete:id=around_test_3" in execution_order
            assert "after_delete" in execution_order
        finally:
            around_delete.disconnect(delete_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_around_update_wraps_update_operation(self) -> None:
        """Test that around_update wraps the update() operation correctly."""
        captured: dict[str, Any] = {}

        @around_update.connect(SignalTestModel)
        async def update_handler(
            sender: type,
            instance: SignalTestModel,
            update_fields: dict[str, Any],
            **kwargs: Any,
        ) -> AsyncGenerator[None, None]:
            captured["before_fields"] = update_fields.copy()
            captured["before_value"] = instance.value
            yield
            captured["after_value"] = instance.value

        try:
            # Create a record
            instance = SignalTestModel(id="around_test_4", name="original", value=0)
            await instance.save()

            # Update it
            instance.name = "updated"
            instance.value = 100
            await instance.update()

            assert "name" in captured["before_fields"]
            assert captured["before_fields"]["name"] == "updated"

            # Cleanup
            await instance.delete()
        finally:
            around_update.disconnect(update_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_around_save_with_try_finally_for_cleanup(self) -> None:
        """Test that around_save can use try/finally for cleanup even on failure."""
        cleanup_called = False
        instance: SignalTestModel | None = None

        @around_save.connect(SignalTestModel)
        async def cleanup_handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            nonlocal cleanup_called
            try:
                yield
            finally:
                cleanup_called = True

        try:
            instance = SignalTestModel(id="around_test_5", name="cleanup_test")
            await instance.save()

            assert cleanup_called

            # Cleanup
            if instance:
                await instance.delete()
        finally:
            around_save.disconnect(cleanup_handler, sender=SignalTestModel)

    @pytest.mark.asyncio
    async def test_around_and_regular_signals_order(self) -> None:
        """Test that signals fire in correct order: pre -> around(before) -> DB -> around(after) -> post."""
        execution_order: list[str] = []

        @pre_save.connect(SignalTestModel)
        async def pre_handler(sender: type, **kwargs: Any) -> None:
            execution_order.append("pre_save")

        @around_save.connect(SignalTestModel)
        async def around_handler(sender: type, **kwargs: Any) -> AsyncGenerator[None, None]:
            execution_order.append("around_before")
            yield
            execution_order.append("around_after")

        @post_save.connect(SignalTestModel)
        async def post_handler(sender: type, **kwargs: Any) -> None:
            execution_order.append("post_save")

        try:
            instance = SignalTestModel(id="around_test_6", name="order_test")
            await instance.save()

            # Expected order: pre_save -> around_before -> (DB) -> around_after -> post_save
            assert execution_order == ["pre_save", "around_before", "around_after", "post_save"]

            # Cleanup
            await instance.delete()
        finally:
            pre_save.disconnect(pre_handler, sender=SignalTestModel)
            around_save.disconnect(around_handler, sender=SignalTestModel)
            post_save.disconnect(post_handler, sender=SignalTestModel)


# =============================================================================
# Cleanup fixture
# =============================================================================


@pytest.fixture(autouse=True)
def cleanup_signals() -> None:
    """Clean up any test handlers after each test."""
    yield
    # Clear any handlers that might have been added during tests
    pre_save.clear()
    post_save.clear()
    pre_delete.clear()
    post_delete.clear()
    pre_update.clear()
    post_update.clear()
    # Clear around signals too
    around_save.clear()
    around_delete.clear()
    around_update.clear()
