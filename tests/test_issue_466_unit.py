"""Unit tests for #466 (v2): DIFF-mode live notifications.

* ``LiveChange.from_dict()`` read the record id only from ``result["id"]``.
  A DIFF result is a list of JSON patches with no id, so ``record_id`` was
  ``""``; the notification names the record in its ``record`` field.
* ``LiveModelStream._to_event()`` validated the empty DIFF result against the
  model, which raised ``ValidationError`` on any required field and ended the
  stream at the first notification.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.live import LiveModelStream
from surreal_sdk.protocol.cbor import RecordId
from surreal_sdk.streaming.live_select import LiveAction, LiveChange


class U466Player(BaseSurrealModel):
    """A model with a required field, which an empty dict cannot satisfy."""

    model_config = SurrealConfigDict(table_name="u466_players")

    id: str | None = None
    name: str
    score: int = Field(default=0)


def _diff_notification(record: Any) -> dict[str, Any]:
    return {
        "action": "UPDATE",
        "id": "00000000-0000-0000-0000-000000000466",
        "record": record,
        "result": [{"op": "replace", "path": "/score", "value": 5}],
    }


class TestFromDictDiff:
    def test_record_id_comes_from_the_record_field(self) -> None:
        change = LiveChange.from_dict(_diff_notification(RecordId("u466_players", "one")))
        assert change.record_id == "u466_players:one"
        assert change.changed_fields == ["score"]
        assert change.result == {}

    def test_record_field_as_a_string(self) -> None:
        change = LiveChange.from_dict(_diff_notification("u466_players:two"))
        assert change.record_id == "u466_players:two"

    def test_result_id_still_wins_for_a_full_record(self) -> None:
        change = LiveChange.from_dict(
            {
                "action": "UPDATE",
                "id": "x",
                "record": RecordId("u466_players", "other"),
                "result": {"id": RecordId("u466_players", "one"), "name": "a"},
            }
        )
        assert change.record_id == "u466_players:one"

    def test_no_record_field_leaves_the_id_empty(self) -> None:
        data = _diff_notification(None)
        del data["record"]
        assert LiveChange.from_dict(data).record_id == ""


class TestToEventDiff:
    async def test_diff_event_carries_only_the_id(self) -> None:
        stream = LiveModelStream(U466Player, None, "u466_players", diff=True)
        change = LiveChange.from_dict(_diff_notification(RecordId("u466_players", "one")))
        event = stream._to_event(change)
        assert event.record_id == "u466_players:one"
        assert event.changed_fields == ["score"]
        assert event.instance.id == "one"
        assert event.action == LiveAction.UPDATE

    async def test_non_diff_event_still_validates_the_record(self) -> None:
        stream = LiveModelStream(U466Player, None, "u466_players", diff=False)
        change = LiveChange(
            id="x",
            action=LiveAction.UPDATE,
            record_id="u466_players:one",
            result={"id": RecordId("u466_players", "one"), "name": "Alice", "score": 3},
        )
        event = stream._to_event(change)
        assert event.instance.name == "Alice"
        assert event.instance.score == 3
        assert event.instance.id == "one"
