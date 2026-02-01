"""Tests for LiveSelectStream and related classes."""

from surreal_sdk.streaming.live_select import (
    LiveChange,
    LiveAction,
    LiveSubscriptionParams,
)


class TestLiveChange:
    """Tests for LiveChange dataclass."""

    def test_from_dict_create(self) -> None:
        """Test parsing a CREATE notification."""
        data = {
            "id": "live-uuid-123",
            "action": "CREATE",
            "result": {"id": "players:abc", "name": "Alice", "is_ready": False},
        }

        change = LiveChange.from_dict(data)

        assert change.id == "live-uuid-123"
        assert change.action == LiveAction.CREATE
        assert change.record_id == "players:abc"
        assert change.result["name"] == "Alice"
        assert change.before is None
        assert change.changed_fields == []

    def test_from_dict_update(self) -> None:
        """Test parsing an UPDATE notification."""
        data = {
            "id": "live-uuid-123",
            "action": "UPDATE",
            "result": {"id": "players:abc", "name": "Alice", "is_ready": True},
        }

        change = LiveChange.from_dict(data)

        assert change.action == LiveAction.UPDATE
        assert change.result["is_ready"] is True

    def test_from_dict_delete(self) -> None:
        """Test parsing a DELETE notification."""
        data = {
            "id": "live-uuid-123",
            "action": "DELETE",
            "result": {"id": "players:abc"},
        }

        change = LiveChange.from_dict(data)

        assert change.action == LiveAction.DELETE
        assert change.record_id == "players:abc"

    def test_from_dict_diff_mode(self) -> None:
        """Test parsing a DIFF mode notification with patches."""
        data = {
            "id": "live-uuid-123",
            "action": "UPDATE",
            "result": [
                {"op": "replace", "path": "/is_ready", "value": True},
                {"op": "replace", "path": "/score", "value": 100},
            ],
        }

        change = LiveChange.from_dict(data)

        assert change.action == LiveAction.UPDATE
        assert "is_ready" in change.changed_fields
        assert "score" in change.changed_fields


class TestLiveSubscriptionParams:
    """Tests for LiveSubscriptionParams dataclass."""

    def test_creation(self) -> None:
        """Test creating subscription params."""
        params = LiveSubscriptionParams(
            table="players",
            where="table_id = $id",
            params={"id": "game_tables:xyz"},
            diff=False,
            callback=None,
            on_reconnect=None,
        )

        assert params.table == "players"
        assert params.where == "table_id = $id"
        assert params.params["id"] == "game_tables:xyz"
        assert params.diff is False


class TestLiveAction:
    """Tests for LiveAction enum."""

    def test_values(self) -> None:
        """Test enum values."""
        assert LiveAction.CREATE == "CREATE"
        assert LiveAction.UPDATE == "UPDATE"
        assert LiveAction.DELETE == "DELETE"

    def test_from_string(self) -> None:
        """Test creating enum from string."""
        assert LiveAction("CREATE") == LiveAction.CREATE
        assert LiveAction("UPDATE") == LiveAction.UPDATE
        assert LiveAction("DELETE") == LiveAction.DELETE
