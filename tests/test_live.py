"""
Unit tests for v0.9.0: Live Models + Change Feed ORM Integration.

These tests verify the ORM-level live streaming classes without
requiring a running SurrealDB instance.
"""

import asyncio
import inspect

import pytest

from src.surreal_orm.live import (
    ChangeModelStream,
    LiveModelStream,
    ModelChangeEvent,
)
from src.surreal_orm.model_base import BaseSurrealModel
from src.surreal_orm.query_set import QuerySet
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
    """Verify __version__ matches pyproject.toml."""

    @staticmethod
    def _read_toml_version(path: str) -> str:
        """Read version from a pyproject.toml file."""
        from pathlib import Path

        for line in Path(path).read_text().splitlines():
            if line.startswith("version = "):
                return line.split('"')[1]
        msg = f"version not found in {path}"
        raise ValueError(msg)

    def test_orm_version(self) -> None:
        """ORM __version__ matches pyproject.toml."""
        from src.surreal_orm import __version__

        expected = self._read_toml_version("pyproject.toml")
        assert __version__ == expected

    def test_sdk_version(self) -> None:
        """SDK __version__ matches its pyproject.toml."""
        from src.surreal_sdk import __version__

        expected = self._read_toml_version("src/surreal_sdk/pyproject.toml")
        assert __version__ == expected


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


def _render(value: object) -> str:
    from src.surreal_sdk.streaming.live_select import LiveSelectStream

    return LiveSelectStream._format_value(value)


def _inline(sql: str, params: dict[str, object]) -> str:
    from src.surreal_sdk.streaming.live_select import LiveSelectStream

    return LiveSelectStream._inline_params_static(sql, params)


class TestFormatValue:
    """``LiveSelectStream._format_value()``: the SurrealQL literal of each value.

    SurrealDB 2.x does not evaluate bound parameters in a ``LIVE SELECT``
    WHERE clause, so each value is written into the query text. The literal
    must designate exactly what the same value bound as a parameter would;
    every form below is checked against a bound value on 2.7.0 by
    ``tests/test_issue_246_v2_integration.py``.
    """

    def test_none(self) -> None:
        assert _render(None) == "NONE"

    def test_bool(self) -> None:
        assert _render(True) == "true"
        assert _render(False) == "false"

    def test_int(self) -> None:
        assert _render(42) == "42"
        assert _render(-5) == "-5"

    def test_int_enum_renders_its_value(self) -> None:
        from enum import Enum

        class Level(int, Enum):
            HIGH = 3

        assert _render(Level.HIGH) == "3"

    def test_float(self) -> None:
        assert _render(3.14) == "3.14"
        assert _render(3.0) == "3.0"
        assert _render(1e20) == "1e+20"
        assert _render(-1e-05) == "-1e-05"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_float_raises(self, value: float) -> None:
        with pytest.raises(TypeError, match="finite"):
            _render(value)

    def test_string(self) -> None:
        assert _render("hello") == "'hello'"

    @pytest.mark.parametrize(
        ("value", "literal"),
        [
            ("it's", "'it\\'s'"),
            ("a\\b", "'a\\\\b'"),
            ("ends with \\", "'ends with \\\\'"),
            ("\\'", "'\\\\\\''"),
            ('say "hi"', "'say \"hi\"'"),
            ("a `b`", "'a `b`'"),
            ("costs $other", "'costs $other'"),
            ("\\1 \\g<0>", "'\\\\1 \\\\g<0>'"),
            ("line\nbreak", "'line\nbreak'"),
        ],
        ids=[
            "quote",
            "backslash",
            "trailing-backslash",
            "backslash-quote",
            "double-quote",
            "backtick",
            "dollar",
            "group-ref",
            "newline",
        ],
    )
    def test_string_literal_keeps_every_character(self, value: str, literal: str) -> None:
        assert _render(value) == literal

    def test_str_enum_renders_its_value(self) -> None:
        from enum import Enum

        class Color(str, Enum):
            RED = "red"

        assert _render(Color.RED) == "'red'"

    def test_decimal(self) -> None:
        from decimal import Decimal

        assert _render(Decimal("9.99")) == "9.99dec"
        assert _render(Decimal("-1.50")) == "-1.50dec"
        assert _render(Decimal("123.4567890123456789012345")) == "123.4567890123456789012345dec"

    def test_decimal_in_scientific_notation_renders_positional(self) -> None:
        """2.7.0 reads ``1E+2dec`` but refuses ``1E+2`` as a decimal parameter; one form for both."""
        from decimal import Decimal

        assert _render(Decimal("1E+2")) == "100dec"
        assert _render(Decimal("1E-5")) == "0.00001dec"

    @pytest.mark.parametrize("digits", ["NaN", "sNaN", "Infinity", "-Infinity"])
    def test_non_finite_decimal_raises(self, digits: str) -> None:
        from decimal import Decimal

        with pytest.raises(TypeError, match="finite"):
            _render(Decimal(digits))

    @pytest.mark.parametrize(
        ("digits", "literal"),
        [
            ("79228162514264337593543950335", "79228162514264337593543950335dec"),
            ("-79228162514264337593543950335", "-79228162514264337593543950335dec"),
            ("1E-28", "0.0000000000000000000000000001dec"),
            ("0.1234567890123456789012345678", "0.1234567890123456789012345678dec"),
        ],
        ids=["max", "min", "smallest-step", "28-places"],
    )
    def test_decimal_at_the_range_bounds(self, digits: str, literal: str) -> None:
        from decimal import Decimal

        assert _render(Decimal(digits)) == literal

    @pytest.mark.parametrize(
        "digits",
        ["1e1000000000", "-1e1000000000", "79228162514264337593543950336", "1E+29"],
        ids=["huge", "huge-negative", "max-plus-one", "1E+29"],
    )
    def test_out_of_range_decimal_raises(self, digits: str) -> None:
        """Out-of-range Decimal values raise instead of being expanded."""
        from decimal import Decimal

        with pytest.raises(ValueError, match="out of range"):
            _render({"price": [Decimal(digits)]})

    def test_extreme_decimal_exponents_never_write_their_digits(self) -> None:
        import tracemalloc
        from decimal import Decimal

        tracemalloc.start()
        try:
            with pytest.raises(ValueError):
                _render(Decimal("1e1000000000"))
            tiny = _render(Decimal("1e-1000000000"))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 1_000_000
        assert tiny == "0.0000000000000000000000000000dec"

    def test_decimal_places_beyond_28_are_rounded_like_a_bound_decimal(self) -> None:
        """Half away from zero at 28 places, as SurrealDB reads a longer value."""
        from decimal import Decimal

        assert _render(Decimal("0.00000000000000000000000000025")) == "0.0000000000000000000000000003dec"
        assert _render(Decimal("-0.00000000000000000000000000025")) == "-0.0000000000000000000000000003dec"
        assert _render(Decimal(1) / Decimal(30)) == "0.0333333333333333333333333333dec"

    def test_uuid(self) -> None:
        from uuid import UUID

        uid = UUID("12345678-1234-5678-1234-567812345678")
        assert _render(uid) == f"u'{uid}'"

    def test_datetime_utc(self) -> None:
        from datetime import UTC, datetime

        assert _render(datetime(2026, 2, 1, 12, 30, tzinfo=UTC)) == "d'2026-02-01T12:30:00+00:00'"

    def test_datetime_with_offset_is_converted_to_utc(self) -> None:
        from datetime import datetime, timedelta, timezone

        when = datetime(2026, 2, 1, 14, 30, 0, 250000, tzinfo=timezone(timedelta(hours=2)))
        assert _render(when) == "d'2026-02-01T12:30:00.250000+00:00'"

    def test_naive_datetime_is_read_as_utc(self) -> None:
        """Like the CBOR encoder's datetime branch: a naive value means UTC."""
        from datetime import datetime

        assert _render(datetime(2026, 2, 1, 12, 30)) == "d'2026-02-01T12:30:00+00:00'"

    def test_date(self) -> None:
        from datetime import date

        assert _render(date(2026, 2, 1)) == "d'2026-02-01'"

    def test_list_and_tuple(self) -> None:
        assert _render([1, "a", None]) == "[1, 'a', NONE]"
        assert _render(("x", 2)) == "['x', 2]"
        assert _render([]) == "[]"

    def test_dict_renders_an_object_literal(self) -> None:
        assert _render({"theme": "dark"}) == "{'theme': 'dark'}"
        assert _render({}) == "{}"

    def test_dict_keys_and_values_render_recursively(self) -> None:
        from decimal import Decimal

        value = {"it's": [1, {"a b": Decimal("1.5")}], "": None}
        assert _render(value) == "{'it\\'s': [1, {'a b': 1.5dec}], '': NONE}"

    def test_dict_with_non_string_key_raises(self) -> None:
        with pytest.raises(TypeError, match="key"):
            _render({1: "one"})

    def test_record_id(self) -> None:
        from src.surreal_sdk.protocol.cbor import RecordId

        assert _render(RecordId(table="users", id="abc123")) == "`users`:`abc123`"

    def test_record_id_int_stays_numeric(self) -> None:
        """``t:123`` is the numeric record; the string ``"123"`` is another one."""
        from src.surreal_sdk.protocol.cbor import RecordId

        assert _render(RecordId(table="t", id=123)) == "`t`:123"
        assert _render(RecordId(table="t", id=-5)) == "`t`:-5"
        assert _render(RecordId(table="t", id="123")) == "`t`:`123`"
        assert _render(RecordId(table="t", id="7qvdzsc14e5clo8sg064")) == "`t`:`7qvdzsc14e5clo8sg064`"

    @pytest.mark.parametrize(
        ("record_id", "literal"),
        [
            ("a b", "`a b`"),
            ("a;b", "`a;b`"),
            ("a'b", "`a'b`"),
            ('a"b', '`a"b`'),
            ("a`b", "`a\\`b`"),
            ("a\\b", "`a\\\\b`"),
            ("a\\", "`a\\\\`"),
            ("a$b", "`a$b`"),
            ("a-b", "`a-b`"),
            ("", "``"),
        ],
        ids=[
            "space",
            "semicolon",
            "quote",
            "double-quote",
            "backtick",
            "backslash",
            "trailing-backslash",
            "dollar",
            "dash",
            "empty",
        ],
    )
    def test_record_id_string_ids(self, record_id: str, literal: str) -> None:
        from src.surreal_sdk.protocol.cbor import RecordId

        assert _render(RecordId(table="t", id=record_id)) == f"`t`:{literal}"
        assert _render(RecordId(table=record_id, id=1)) == f"{literal}:1"

    def test_record_id_with_array_object_and_uuid_ids(self) -> None:
        from uuid import UUID

        from src.surreal_sdk.protocol.cbor import RecordId

        uid = UUID("12345678-1234-5678-1234-567812345678")
        assert _render(RecordId(table="t", id=["a", 1])) == "`t`:['a', 1]"
        assert _render(RecordId(table="t", id={"k": "v"})) == "`t`:{'k': 'v'}"
        assert _render(RecordId(table="t", id=uid)) == f"`t`:u'{uid}'"

    @pytest.mark.parametrize("record_id", [True, 1.5, None])
    def test_record_id_with_unsupported_id_raises(self, record_id: object) -> None:
        from src.surreal_sdk.protocol.cbor import RecordId

        with pytest.raises(TypeError):
            _render(RecordId(table="t", id=record_id))

    def test_record_id_with_non_string_table_raises(self) -> None:
        from src.surreal_sdk.protocol.cbor import RecordId

        with pytest.raises(TypeError):
            _render(RecordId(table=1, id="a"))  # type: ignore[arg-type]

    def test_unsupported_types_raise(self) -> None:
        from src.surreal_sdk.protocol.cbor import Duration, Table

        for value in [{1, 2}, frozenset({1}), b"bytes", Duration("1h"), Table("users"), object(), 1j]:
            with pytest.raises(TypeError, match="live"):
                _render(value)


class TestInlineParams:
    """``LiveSelectStream._inline_params_static()``: one substitution pass."""

    def test_single_param(self) -> None:
        sql = "LIVE SELECT * FROM users WHERE role = $_f0"
        assert _inline(sql, {"_f0": "admin"}) == "LIVE SELECT * FROM users WHERE role = 'admin'"

    def test_multiple_params(self) -> None:
        sql = "LIVE SELECT * FROM users WHERE role = $_f0 AND age >= $_f1"
        assert _inline(sql, {"_f0": "admin", "_f1": 18}) == "LIVE SELECT * FROM users WHERE role = 'admin' AND age >= 18"

    def test_longer_name_is_not_split(self) -> None:
        """``$_f10`` is its own name, never ``$_f1`` followed by ``0``."""
        assert _inline("WHERE x = $_f1 AND y = $_f10", {"_f1": "a", "_f10": "b"}) == "WHERE x = 'a' AND y = 'b'"
        assert _inline("WHERE y = $_f10", {"_f1": "a"}) == "WHERE y = $_f10"

    def test_inserted_text_is_not_rescanned(self) -> None:
        """A ``$name`` inside a value is part of the string, whatever the order of the keys."""
        params = {"a": "has $b", "b": 1}
        assert _inline("x = $a AND y = $b", params) == "x = 'has $b' AND y = 1"
        assert _inline("x = $a AND y = $b", dict(reversed(params.items()))) == "x = 'has $b' AND y = 1"

    def test_inserted_text_is_not_a_replacement_pattern(self) -> None:
        """Backslashes and group references in a value come through unchanged."""
        assert _inline("x = $a", {"a": "C:\\dir\\"}) == "x = 'C:\\\\dir\\\\'"
        assert _inline("x = $a", {"a": "\\g<0> \\1"}) == "x = '\\\\g<0> \\\\1'"

    def test_names_without_a_value_are_left_as_written(self) -> None:
        assert _inline("x = $auth.id AND y = $_f0 AND z = $", {"_f0": 1}) == "x = $auth.id AND y = 1 AND z = $"

    def test_unsupported_value_raises(self) -> None:
        with pytest.raises(TypeError):
            _inline("x = $a", {"a": {1, 2}})

    async def test_resubscribe_sends_the_same_query(self) -> None:
        """After a reconnect the re-created LIVE SELECT carries the same literals."""
        from decimal import Decimal

        from src.surreal_sdk.connection.websocket import WebSocketConnection
        from src.surreal_sdk.protocol.cbor import RecordId
        from src.surreal_sdk.streaming.live_select import LiveSelectStream, LiveSubscriptionParams
        from src.surreal_sdk.types import QueryResponse

        params = {"_f0": {"theme": "dark"}, "_f1": Decimal("9.99"), "_f2": RecordId("a", "x`y")}
        where = "prefs = $_f0 AND price = $_f1 AND author = $_f2"
        sent: list[str] = []

        async def fake_query(sql: str, vars: dict[str, object] | None = None) -> QueryResponse:
            sent.append(sql)
            return QueryResponse.from_rpc_result([{"status": "OK", "result": "new-live-id", "time": "1ms"}])

        conn = WebSocketConnection("ws://localhost:8000", "ns", "db")
        conn.query = fake_query  # type: ignore[method-assign]
        stream = LiveSelectStream(conn, "items", where=where, params=params)

        await stream.start()
        await conn._resubscribe_one(
            LiveSubscriptionParams(table="items", where=where, params=params, diff=False, callback=None, on_reconnect=None)
        )

        expected = "LIVE SELECT * FROM items WHERE prefs = {'theme': 'dark'} AND price = 9.99dec AND author = `a`:`x\\`y`"
        assert sent == [expected, expected]


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
