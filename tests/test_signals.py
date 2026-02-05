"""Tests for Model Signals."""

from __future__ import annotations

import pytest
from typing import Any
from pydantic import Field

from surreal_orm import (
    BaseSurrealModel,
    SurrealConfigDict,
    Signal,
    pre_save,
    post_save,
    pre_delete,
    post_delete,
    pre_update,
    post_update,
)


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
            received.append({
                "sender": sender,
                "instance_name": instance.name,
                "created": created,
            })

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
