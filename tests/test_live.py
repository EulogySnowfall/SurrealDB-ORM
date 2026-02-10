"""
Unit tests for v0.9.0: Live Models + Change Feed ORM Integration.

These tests verify the ORM-level live streaming classes without
requiring a running SurrealDB instance.
"""

import asyncio
import inspect

import pytest

from src.surreal_orm.model_base import BaseSurrealModel
from src.surreal_orm.query_set import QuerySet
from src.surreal_orm.live import (
    LiveModelStream,
    ChangeModelStream,
    ModelChangeEvent,
)
from src.surreal_sdk.streaming.live_select import LiveAction, LiveChange


# ==================== Test Models ====================


class PlayerModel(BaseSurrealModel):
    id: str | None = None
    name: str = ""
    age: int = 0
    role: str = "player"


# ==================== ModelChangeEvent ====================


class TestModelChangeEvent:
    """Tests for ModelChangeEvent dataclass."""

    def test_create_event(self) -> None:
        """ModelChangeEvent can be constructed with all fields."""
        player = PlayerModel(id="abc", name="Alice", age=25)
        event = ModelChangeEvent(
            action=LiveAction.CREATE,
            instance=player,
            record_id="players:abc",
        )
        assert event.action == LiveAction.CREATE
        assert event.instance.name == "Alice"
        assert event.record_id == "players:abc"
        assert event.changed_fields == []
        assert event.raw == {}

    def test_event_with_changed_fields(self) -> None:
        """ModelChangeEvent stores changed_fields from DIFF mode."""
        player = PlayerModel(id="abc", name="Alice")
        event = ModelChangeEvent(
            action=LiveAction.UPDATE,
            instance=player,
            record_id="players:abc",
            changed_fields=["name", "age"],
            raw={"id": "players:abc", "name": "Alice", "age": 26},
        )
        assert event.changed_fields == ["name", "age"]
        assert event.raw["age"] == 26

    def test_event_delete_action(self) -> None:
        """ModelChangeEvent supports DELETE action."""
        player = PlayerModel(id="abc")
        event = ModelChangeEvent(
            action=LiveAction.DELETE,
            instance=player,
            record_id="players:abc",
        )
        assert event.action == LiveAction.DELETE


# ==================== LiveModelStream ====================


class TestLiveModelStream:
    """Tests for LiveModelStream class."""

    def test_init_accepts_none_connection(self) -> None:
        """LiveModelStream accepts connection=None (lazy resolution)."""
        stream = LiveModelStream(
            model=PlayerModel,
            connection=None,
            table="players",
        )
        assert stream._connection is None
        assert stream._table == "players"

    def test_init_stores_parameters(self) -> None:
        """LiveModelStream stores all initialization parameters."""
        stream = LiveModelStream(
            model=PlayerModel,
            connection=None,
            table="players",
            where="role = $_f0",
            params={"_f0": "admin"},
            diff=True,
            auto_resubscribe=False,
        )
        assert stream._where == "role = $_f0"
        assert stream._params == {"_f0": "admin"}
        assert stream._diff is True
        assert stream._auto_resubscribe is False

    def test_is_active_false_before_start(self) -> None:
        """is_active returns False before stream is started."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        assert stream.is_active is False

    def test_live_id_none_before_start(self) -> None:
        """live_id returns None before stream is started."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        assert stream.live_id is None

    def test_to_event_create(self) -> None:
        """_to_event converts CREATE LiveChange to ModelChangeEvent."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        change = LiveChange(
            id="live-uuid",
            action=LiveAction.CREATE,
            record_id="players:abc",
            result={"id": "abc", "name": "Alice", "age": 25, "role": "admin"},
        )
        event = stream._to_event(change)
        assert event.action == LiveAction.CREATE
        assert event.instance.name == "Alice"
        assert event.record_id == "players:abc"

    def test_to_event_update(self) -> None:
        """_to_event converts UPDATE LiveChange to ModelChangeEvent."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        change = LiveChange(
            id="live-uuid",
            action=LiveAction.UPDATE,
            record_id="players:abc",
            result={"id": "abc", "name": "Bob", "age": 30, "role": "player"},
            changed_fields=["name"],
        )
        event = stream._to_event(change)
        assert event.action == LiveAction.UPDATE
        assert event.instance.name == "Bob"
        assert event.changed_fields == ["name"]

    def test_to_event_delete_empty_result(self) -> None:
        """_to_event handles DELETE with empty result (constructs minimal instance)."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        change = LiveChange(
            id="live-uuid",
            action=LiveAction.DELETE,
            record_id="players:abc",
            result={},
        )
        event = stream._to_event(change)
        assert event.action == LiveAction.DELETE
        assert event.record_id == "players:abc"

    def test_to_event_delete_with_result(self) -> None:
        """_to_event handles DELETE with result data."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        change = LiveChange(
            id="live-uuid",
            action=LiveAction.DELETE,
            record_id="players:abc",
            result={"id": "abc", "name": "Alice", "age": 25, "role": "player"},
        )
        event = stream._to_event(change)
        assert event.action == LiveAction.DELETE
        assert event.instance.name == "Alice"

    def test_aiter_returns_self(self) -> None:
        """__aiter__ returns self."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        assert stream.__aiter__() is stream

    def test_anext_raises_when_no_stream(self) -> None:
        """__anext__ raises StopAsyncIteration when stream is None."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        with pytest.raises(StopAsyncIteration):
            asyncio.run(stream.__anext__())


# ==================== ChangeModelStream ====================


class TestChangeModelStream:
    """Tests for ChangeModelStream class."""

    def test_init_accepts_none_connection(self) -> None:
        """ChangeModelStream accepts connection=None (lazy resolution)."""
        stream = ChangeModelStream(
            model=PlayerModel,
            connection=None,
            table="players",
        )
        assert stream._connection is None
        assert stream._table == "players"

    def test_init_stores_parameters(self) -> None:
        """ChangeModelStream stores all initialization parameters."""
        stream = ChangeModelStream(
            model=PlayerModel,
            connection=None,
            table="players",
            since="2026-01-01",
            poll_interval=0.5,
            batch_size=50,
        )
        assert stream._since == "2026-01-01"
        assert stream._poll_interval == 0.5
        assert stream._batch_size == 50

    def test_cursor_none_before_start(self) -> None:
        """cursor returns None before feed is initialized."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        assert stream.cursor is None

    def test_stop_before_init(self) -> None:
        """stop() is safe to call before feed is initialized."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        stream.stop()  # Should not raise

    def test_parse_change_create(self) -> None:
        """_parse_change parses create actions."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                {"create": {"id": "players:abc", "name": "Alice", "age": 25, "role": "player"}},
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 1
        assert events[0].action == LiveAction.CREATE
        assert events[0].instance.name == "Alice"
        assert events[0].record_id == "players:abc"

    def test_parse_change_update(self) -> None:
        """_parse_change parses update actions."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                {"update": {"id": "players:abc", "name": "Bob", "age": 30, "role": "admin"}},
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 1
        assert events[0].action == LiveAction.UPDATE
        assert events[0].instance.name == "Bob"

    def test_parse_change_delete(self) -> None:
        """_parse_change parses delete actions."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                {"delete": {"id": "players:abc", "name": "Alice", "age": 25, "role": "player"}},
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 1
        assert events[0].action == LiveAction.DELETE

    def test_parse_change_multiple(self) -> None:
        """_parse_change handles multiple changes in one record."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                {"create": {"id": "players:a", "name": "Alice", "age": 25, "role": "player"}},
                {"update": {"id": "players:b", "name": "Bob", "age": 30, "role": "admin"}},
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 2
        assert events[0].action == LiveAction.CREATE
        assert events[1].action == LiveAction.UPDATE

    def test_parse_change_skips_define_table(self) -> None:
        """_parse_change skips define_table entries."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                {"define_table": {"name": "players"}},
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 0

    def test_parse_change_empty(self) -> None:
        """_parse_change handles empty changes list."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        events = stream._parse_change({"changes": []})
        assert len(events) == 0

    def test_parse_change_no_changes_key(self) -> None:
        """_parse_change handles missing changes key."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        events = stream._parse_change({})
        assert len(events) == 0

    def test_aiter_returns_self(self) -> None:
        """__aiter__ returns self."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        assert stream.__aiter__() is stream


# ==================== QuerySet.live() ====================


class TestQuerySetLive:
    """Tests for QuerySet.live() method."""

    def test_live_method_exists(self) -> None:
        """QuerySet must have a live() method."""
        assert hasattr(QuerySet, "live")

    def test_live_returns_live_model_stream(self) -> None:
        """live() returns a LiveModelStream instance."""
        stream = PlayerModel.objects().live()
        assert isinstance(stream, LiveModelStream)

    def test_live_passes_table_name(self) -> None:
        """live() passes the model's table name to LiveModelStream."""
        stream = PlayerModel.objects().live()
        assert stream._table == PlayerModel.get_table_name()

    def test_live_passes_filters_as_where(self) -> None:
        """live() translates QuerySet filters to WHERE clause."""
        stream = PlayerModel.objects().filter(role="admin").live()
        assert stream._where is not None
        assert "role" in stream._where
        assert "$_f0" in stream._where

    def test_live_passes_filter_variables_as_params(self) -> None:
        """live() passes filter variables as params to LiveModelStream."""
        stream = PlayerModel.objects().filter(role="admin", age__gte=18).live()
        assert stream._params is not None
        assert "_f0" in stream._params
        assert "_f1" in stream._params

    def test_live_no_filters_no_where(self) -> None:
        """live() with no filters sets where=None."""
        stream = PlayerModel.objects().live()
        assert stream._where is None

    def test_live_passes_diff_param(self) -> None:
        """live() passes diff parameter through."""
        stream = PlayerModel.objects().live(diff=True)
        assert stream._diff is True

    def test_live_passes_auto_resubscribe_param(self) -> None:
        """live() passes auto_resubscribe parameter through."""
        stream = PlayerModel.objects().live(auto_resubscribe=False)
        assert stream._auto_resubscribe is False

    def test_live_default_auto_resubscribe(self) -> None:
        """live() defaults auto_resubscribe to True."""
        stream = PlayerModel.objects().live()
        assert stream._auto_resubscribe is True

    def test_live_connection_is_none(self) -> None:
        """live() sets connection=None for lazy resolution."""
        stream = PlayerModel.objects().live()
        assert stream._connection is None

    def test_live_with_q_objects(self) -> None:
        """live() works with Q object filters."""
        from src.surreal_orm.q import Q

        stream = (
            PlayerModel.objects()
            .filter(
                Q(name="alice") | Q(name="bob"),
            )
            .live()
        )
        assert stream._where is not None
        assert "OR" in stream._where

    def test_live_with_variables(self) -> None:
        """live() includes explicit variables in params."""
        stream = PlayerModel.objects().filter(role="$user_role").variables(user_role="admin").live()
        assert stream._params is not None
        assert "user_role" in stream._params


# ==================== QuerySet.changes() ====================


class TestQuerySetChanges:
    """Tests for QuerySet.changes() method."""

    def test_changes_method_exists(self) -> None:
        """QuerySet must have a changes() method."""
        assert hasattr(QuerySet, "changes")

    def test_changes_returns_change_model_stream(self) -> None:
        """changes() returns a ChangeModelStream instance."""
        stream = PlayerModel.objects().changes()
        assert isinstance(stream, ChangeModelStream)

    def test_changes_passes_table_name(self) -> None:
        """changes() passes the model's table name."""
        stream = PlayerModel.objects().changes()
        assert stream._table == PlayerModel.get_table_name()

    def test_changes_passes_since(self) -> None:
        """changes() passes the since parameter."""
        stream = PlayerModel.objects().changes(since="2026-01-01")
        assert stream._since == "2026-01-01"

    def test_changes_passes_poll_interval(self) -> None:
        """changes() passes poll_interval parameter."""
        stream = PlayerModel.objects().changes(poll_interval=0.5)
        assert stream._poll_interval == 0.5

    def test_changes_passes_batch_size(self) -> None:
        """changes() passes batch_size parameter."""
        stream = PlayerModel.objects().changes(batch_size=50)
        assert stream._batch_size == 50

    def test_changes_connection_is_none(self) -> None:
        """changes() sets connection=None for lazy resolution."""
        stream = PlayerModel.objects().changes()
        assert stream._connection is None


# ==================== Signals ====================


class TestPostLiveChangeSignal:
    """Tests for post_live_change signal."""

    def test_signal_exists(self) -> None:
        """post_live_change signal is defined."""
        from src.surreal_orm.signals import post_live_change

        assert post_live_change.name == "post_live_change"

    def test_signal_importable_from_orm(self) -> None:
        """post_live_change is importable from surreal_orm."""
        from src.surreal_orm import post_live_change

        assert post_live_change is not None

    def test_signal_connect_decorator(self) -> None:
        """post_live_change.connect() works as a decorator."""
        from src.surreal_orm.signals import post_live_change

        @post_live_change.connect(PlayerModel)
        async def handler(sender: type, **kwargs: object) -> None:
            pass

        assert post_live_change.has_receivers(PlayerModel)

        # Cleanup
        post_live_change.disconnect(handler, PlayerModel)


# ==================== Exports ====================


class TestExports:
    """Tests for public API exports."""

    def test_live_model_stream_exported(self) -> None:
        """LiveModelStream is importable from surreal_orm."""
        from src.surreal_orm import LiveModelStream

        assert LiveModelStream is not None

    def test_model_change_event_exported(self) -> None:
        """ModelChangeEvent is importable from surreal_orm."""
        from src.surreal_orm import ModelChangeEvent

        assert ModelChangeEvent is not None

    def test_change_model_stream_exported(self) -> None:
        """ChangeModelStream is importable from surreal_orm."""
        from src.surreal_orm import ChangeModelStream

        assert ChangeModelStream is not None

    def test_live_action_exported(self) -> None:
        """LiveAction is importable from surreal_orm."""
        from src.surreal_orm import LiveAction

        assert LiveAction.CREATE == "CREATE"
        assert LiveAction.UPDATE == "UPDATE"
        assert LiveAction.DELETE == "DELETE"

    def test_post_live_change_exported(self) -> None:
        """post_live_change is importable from surreal_orm."""
        from src.surreal_orm import post_live_change

        assert post_live_change is not None


# ==================== ConnectionManager.get_ws_client ====================


class TestConnectionManagerWebSocket:
    """Tests for WebSocket support in SurrealDBConnectionManager."""

    def test_get_ws_client_exists(self) -> None:
        """SurrealDBConnectionManager must have get_ws_client classmethod."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        assert hasattr(SurrealDBConnectionManager, "get_ws_client")
        assert inspect.iscoroutinefunction(SurrealDBConnectionManager.get_ws_client)

    def test_get_ws_client_raises_without_connection(self) -> None:
        """get_ws_client() raises ValueError without set_connection()."""
        from src.surreal_orm.connection_manager import SurrealDBConnectionManager

        # Save and clear registries (including cached clients) to test the error path
        saved_configs = dict(SurrealDBConnectionManager._configs)
        saved_clients = dict(SurrealDBConnectionManager._clients)
        saved_ws = dict(SurrealDBConnectionManager._ws_clients)
        SurrealDBConnectionManager._configs.clear()
        SurrealDBConnectionManager._clients.clear()
        SurrealDBConnectionManager._ws_clients.clear()
        try:
            with pytest.raises(ValueError, match="not configured"):
                asyncio.run(SurrealDBConnectionManager.get_ws_client())
        finally:
            SurrealDBConnectionManager._configs.update(saved_configs)
            SurrealDBConnectionManager._clients.update(saved_clients)
            SurrealDBConnectionManager._ws_clients.update(saved_ws)


# ==================== Version ====================


class TestVersion:
    """Verify version was bumped."""

    def test_orm_version(self) -> None:
        """ORM version is 0.10.0."""
        from src.surreal_orm import __version__

        assert __version__ == "0.10.0"

    def test_sdk_version(self) -> None:
        """SDK version is 0.10.0."""
        from src.surreal_sdk import __version__

        assert __version__ == "0.10.0"


# ==================== LiveModelStream Edge Cases ====================


class TestLiveModelStreamEdgeCases:
    """Additional edge case tests for LiveModelStream."""

    def test_to_event_from_db_returns_list(self) -> None:
        """_to_event handles from_db returning a list (standard behavior)."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        change = LiveChange(
            id="live-uuid",
            action=LiveAction.CREATE,
            record_id="players:xyz",
            result={"id": "xyz", "name": "Charlie", "age": 30, "role": "admin"},
        )
        event = stream._to_event(change)
        assert isinstance(event.instance, PlayerModel)
        assert event.instance.name == "Charlie"

    def test_to_event_preserves_raw_data(self) -> None:
        """_to_event stores the raw result dict."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        raw = {"id": "abc", "name": "Alice", "age": 25, "role": "player"}
        change = LiveChange(
            id="live-uuid",
            action=LiveAction.CREATE,
            record_id="players:abc",
            result=raw,
        )
        event = stream._to_event(change)
        assert event.raw == raw

    def test_to_event_changed_fields_propagated(self) -> None:
        """_to_event propagates changed_fields from LiveChange."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        change = LiveChange(
            id="live-uuid",
            action=LiveAction.UPDATE,
            record_id="players:abc",
            result={"id": "abc", "name": "Bob", "age": 35, "role": "player"},
            changed_fields=["age", "name"],
        )
        event = stream._to_event(change)
        assert "age" in event.changed_fields
        assert "name" in event.changed_fields

    def test_stop_before_start_is_safe(self) -> None:
        """stop() is safe to call before the stream is started."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        asyncio.run(stream.stop())  # Should not raise

    def test_aexit_before_aenter_is_safe(self) -> None:
        """__aexit__ is safe to call without __aenter__."""
        stream = LiveModelStream(model=PlayerModel, connection=None, table="players")
        asyncio.run(stream.__aexit__(None, None, None))  # Should not raise


# ==================== ChangeModelStream Edge Cases ====================


class TestChangeModelStreamEdgeCases:
    """Additional edge case tests for ChangeModelStream."""

    def test_parse_change_unknown_action_key(self) -> None:
        """_parse_change ignores unknown action keys."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                {"unknown_action": {"id": "players:abc"}},
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 0

    def test_parse_change_non_dict_item(self) -> None:
        """_parse_change skips non-dict items in changes list."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                "not a dict",
                42,
                None,
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 0

    def test_parse_change_non_dict_record(self) -> None:
        """_parse_change skips entries where the action value is not a dict."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                {"create": "not a dict"},
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 0

    def test_parse_change_record_without_id(self) -> None:
        """_parse_change handles records missing the id field."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        change = {
            "changes": [
                {"create": {"name": "NoId", "age": 20, "role": "player"}},
            ],
        }
        events = stream._parse_change(change)
        assert len(events) == 1
        assert events[0].record_id == ""

    def test_since_parameter_stored(self) -> None:
        """ChangeModelStream stores the since parameter for later use."""
        from datetime import datetime

        now = datetime(2026, 1, 1, 0, 0, 0)
        stream = ChangeModelStream(
            model=PlayerModel,
            connection=None,
            table="players",
            since=now,
        )
        assert stream._since == now

    def test_defaults(self) -> None:
        """ChangeModelStream has sensible defaults."""
        stream = ChangeModelStream(model=PlayerModel, connection=None, table="players")
        assert stream._since is None
        assert stream._poll_interval == 0.1
        assert stream._batch_size == 100


# ==================== QuerySet Integration ====================


class TestQuerySetLiveExtended:
    """Extended tests for QuerySet.live() and .changes() methods."""

    def test_live_with_multiple_filters(self) -> None:
        """live() with multiple filter conditions creates a combined WHERE clause."""
        stream = (
            PlayerModel.objects()
            .filter(
                role="admin",
                age__gte=18,
                name__startswith="A",
            )
            .live()
        )
        assert stream._where is not None
        assert "role" in stream._where
        assert "age" in stream._where
        assert "name" in stream._where

    def test_live_with_explicit_variables_merged(self) -> None:
        """live() merges explicit variables with filter variables."""
        stream = PlayerModel.objects().filter(role="$user_role").variables(user_role="admin").live()
        assert stream._params is not None
        assert "user_role" in stream._params
        assert stream._params["user_role"] == "admin"

    def test_changes_defaults(self) -> None:
        """changes() uses sensible defaults when called without arguments."""
        stream = PlayerModel.objects().changes()
        assert stream._since is None
        assert stream._poll_interval == 0.1
        assert stream._batch_size == 100

    def test_changes_with_datetime_since(self) -> None:
        """changes() accepts datetime objects for the since parameter."""
        from datetime import datetime

        now = datetime(2026, 2, 1)
        stream = PlayerModel.objects().changes(since=now)
        assert stream._since == now

    def test_live_model_passed(self) -> None:
        """live() passes the correct model class to the stream."""
        stream = PlayerModel.objects().live()
        assert stream._model is PlayerModel

    def test_changes_model_passed(self) -> None:
        """changes() passes the correct model class to the stream."""
        stream = PlayerModel.objects().changes()
        assert stream._model is PlayerModel


# ==================== _format_value & _parse_id ====================


class TestFormatValue:
    """Tests for LiveSelectStream._format_value() inline param formatting."""

    def test_none(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        assert LiveSelectStream._format_value(None) == "NONE"

    def test_bool_true(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        assert LiveSelectStream._format_value(True) == "true"

    def test_bool_false(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        assert LiveSelectStream._format_value(False) == "false"

    def test_int(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        assert LiveSelectStream._format_value(42) == "42"

    def test_float(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        assert LiveSelectStream._format_value(3.14) == "3.14"

    def test_string(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        assert LiveSelectStream._format_value("hello") == "'hello'"

    def test_string_with_quotes(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        assert LiveSelectStream._format_value("it's") == "'it\\'s'"

    def test_list(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        assert LiveSelectStream._format_value([1, "a"]) == "[1, 'a']"

    def test_record_id(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream
        from src.surreal_sdk.protocol.cbor import RecordId

        rid = RecordId(table="users", id="abc123")
        assert LiveSelectStream._format_value(rid) == "users:abc123"

    def test_uuid(self) -> None:
        from uuid import UUID

        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        uid = UUID("12345678-1234-5678-1234-567812345678")
        assert LiveSelectStream._format_value(uid) == f"u'{uid}'"

    def test_datetime(self) -> None:
        from datetime import datetime

        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        dt = datetime(2026, 2, 1, 12, 30, 0)
        assert LiveSelectStream._format_value(dt) == f"d'{dt.isoformat()}'"

    def test_date(self) -> None:
        from datetime import date

        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        d = date(2026, 2, 1)
        assert LiveSelectStream._format_value(d) == f"d'{d.isoformat()}'"


class TestInlineParams:
    """Tests for LiveSelectStream._inline_params_static()."""

    def test_single_param(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        sql = "LIVE SELECT * FROM users WHERE role = $_f0"
        result = LiveSelectStream._inline_params_static(sql, {"_f0": "admin"})
        assert result == "LIVE SELECT * FROM users WHERE role = 'admin'"

    def test_multiple_params(self) -> None:
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        sql = "LIVE SELECT * FROM users WHERE role = $_f0 AND age >= $_f1"
        result = LiveSelectStream._inline_params_static(sql, {"_f0": "admin", "_f1": 18})
        assert result == "LIVE SELECT * FROM users WHERE role = 'admin' AND age >= 18"

    def test_longer_key_first(self) -> None:
        """$_f10 should be replaced before $_f1 to avoid partial replacement."""
        from src.surreal_sdk.streaming.live_select import LiveSelectStream

        sql = "WHERE x = $_f1 AND y = $_f10"
        result = LiveSelectStream._inline_params_static(sql, {"_f1": "a", "_f10": "b"})
        assert result == "WHERE x = 'a' AND y = 'b'"


class TestParseId:
    """Tests for LiveModelStream._parse_id()."""

    def test_with_table_prefix(self) -> None:
        assert LiveModelStream._parse_id("users:abc123") == "abc123"

    def test_without_prefix(self) -> None:
        assert LiveModelStream._parse_id("abc123") == "abc123"

    def test_empty_string(self) -> None:
        assert LiveModelStream._parse_id("") == ""

    def test_multiple_colons(self) -> None:
        assert LiveModelStream._parse_id("table:complex:id") == "complex:id"


# ==================== __all__ exports ====================


class TestModuleExports:
    """Test that live.py exports the correct symbols."""

    def test_all_exports(self) -> None:
        """live.py __all__ contains the expected symbols."""
        from src.surreal_orm.live import __all__

        assert "ModelChangeEvent" in __all__
        assert "LiveModelStream" in __all__
        assert "ChangeModelStream" in __all__
