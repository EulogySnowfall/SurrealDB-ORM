"""
Tests for DEFINE EVENT support — operations, parser, state diff, and introspection.
"""

from __future__ import annotations

from surreal_orm.migrations.define_parser import parse_define_event
from surreal_orm.migrations.operations import DefineEvent, RemoveEvent
from surreal_orm.migrations.state import EventState, SchemaState, TableState

# ---------------------------------------------------------------------------
# DefineEvent operation
# ---------------------------------------------------------------------------


class TestDefineEvent:
    """Tests for the DefineEvent migration operation."""

    def test_forwards_basic(self) -> None:
        op = DefineEvent(
            name="audit_create",
            table="users",
            when="$event = 'CREATE'",
            then="CREATE audit_log SET table = 'users', action = 'create'",
        )
        sql = op.forwards()
        assert sql == (
            "DEFINE EVENT audit_create ON users "
            "WHEN $event = 'CREATE' "
            "THEN (CREATE audit_log SET table = 'users', action = 'create');"
        )

    def test_forwards_with_comment(self) -> None:
        op = DefineEvent(
            name="notify",
            table="orders",
            when="$event = 'UPDATE'",
            then="http::post('https://hooks.example.com', $after)",
            comment="Webhook notification",
        )
        sql = op.forwards()
        assert "COMMENT 'Webhook notification'" in sql
        assert "DEFINE EVENT notify ON orders" in sql

    def test_backwards(self) -> None:
        op = DefineEvent(
            name="audit_create",
            table="users",
            when="$event = 'CREATE'",
            then="CREATE audit_log SET action = $event",
        )
        assert op.backwards() == "REMOVE EVENT audit_create ON users;"

    def test_describe(self) -> None:
        op = DefineEvent(name="evt", table="tbl", when="true", then="true")
        assert op.describe() == "Define event evt on tbl"

    def test_reversible(self) -> None:
        op = DefineEvent(name="evt", table="tbl", when="true", then="true")
        assert op.reversible is True


# ---------------------------------------------------------------------------
# RemoveEvent operation
# ---------------------------------------------------------------------------


class TestRemoveEvent:
    """Tests for the RemoveEvent migration operation."""

    def test_forwards(self) -> None:
        op = RemoveEvent(name="audit_create", table="users")
        assert op.forwards() == "REMOVE EVENT audit_create ON users;"

    def test_backwards_empty(self) -> None:
        op = RemoveEvent(name="audit_create", table="users")
        assert op.backwards() == ""

    def test_not_reversible(self) -> None:
        op = RemoveEvent(name="evt", table="tbl")
        assert op.reversible is False

    def test_describe(self) -> None:
        op = RemoveEvent(name="evt", table="tbl")
        assert op.describe() == "Remove event evt from tbl"


# ---------------------------------------------------------------------------
# parse_define_event
# ---------------------------------------------------------------------------


class TestParseDefineEvent:
    """Tests for parse_define_event parser."""

    def test_basic_event(self) -> None:
        stmt = (
            "DEFINE EVENT audit_create ON users "
            "WHEN $event = 'CREATE' "
            "THEN (CREATE audit_log SET table = 'users', action = $event)"
        )
        result = parse_define_event(stmt)
        assert isinstance(result, EventState)
        assert result.name == "audit_create"
        assert result.table == "users"
        assert result.when == "$event = 'CREATE'"
        assert "CREATE audit_log" in result.then

    def test_with_table_keyword(self) -> None:
        stmt = "DEFINE EVENT notify ON TABLE orders WHEN $event = 'UPDATE' THEN (http::post('https://example.com', $after))"
        result = parse_define_event(stmt)
        assert result.name == "notify"
        assert result.table == "orders"

    def test_complex_when_condition(self) -> None:
        stmt = (
            "DEFINE EVENT email_change ON users "
            "WHEN $before.email != $after.email "
            "THEN (CREATE audit_log SET old = $before.email, new = $after.email)"
        )
        result = parse_define_event(stmt)
        assert result.when == "$before.email != $after.email"

    def test_multiline_then(self) -> None:
        stmt = (
            "DEFINE EVENT complex ON users "
            "WHEN $event = 'CREATE' "
            "THEN (CREATE audit_log SET "
            "table = 'users', action = $event, at = time::now())"
        )
        result = parse_define_event(stmt)
        assert "time::now()" in result.then

    def test_if_not_exists(self) -> None:
        stmt = (
            "DEFINE EVENT IF NOT EXISTS my_event ON users "
            "WHEN $event = 'DELETE' "
            "THEN (DELETE audit_log WHERE record = $before.id)"
        )
        result = parse_define_event(stmt)
        assert result.name == "my_event"
        assert result.table == "users"


# ---------------------------------------------------------------------------
# EventState equality
# ---------------------------------------------------------------------------


class TestEventState:
    """Tests for EventState dataclass."""

    def test_equality(self) -> None:
        a = EventState(name="evt", table="tbl", when="$event = 'CREATE'", then="true")
        b = EventState(name="evt", table="tbl", when="$event = 'CREATE'", then="true")
        assert a == b

    def test_inequality_name(self) -> None:
        a = EventState(name="evt1", table="tbl", when="cond", then="action")
        b = EventState(name="evt2", table="tbl", when="cond", then="action")
        assert a != b

    def test_inequality_when(self) -> None:
        a = EventState(name="evt", table="tbl", when="cond1", then="action")
        b = EventState(name="evt", table="tbl", when="cond2", then="action")
        assert a != b

    def test_inequality_then(self) -> None:
        a = EventState(name="evt", table="tbl", when="cond", then="action1")
        b = EventState(name="evt", table="tbl", when="cond", then="action2")
        assert a != b

    def test_not_equal_to_other_types(self) -> None:
        a = EventState(name="evt", table="tbl", when="cond", then="action")
        assert a != "not an event"


# ---------------------------------------------------------------------------
# SchemaState diff — event operations
# ---------------------------------------------------------------------------


class TestEventStateDiff:
    """Tests for event diffing in SchemaState.diff()."""

    def _make_state(self, tables: dict[str, TableState]) -> SchemaState:
        state = SchemaState()
        state.tables = tables
        return state

    def test_new_table_with_events(self) -> None:
        """Creating a new table should also emit DefineEvent for its events."""
        target = self._make_state(
            {
                "users": TableState(
                    name="users",
                    events={
                        "audit": EventState(
                            name="audit",
                            table="users",
                            when="$event = 'CREATE'",
                            then="CREATE log SET action = 'create'",
                        )
                    },
                )
            }
        )
        current = self._make_state({})
        ops = current.diff(target)

        event_ops = [op for op in ops if isinstance(op, DefineEvent)]
        assert len(event_ops) == 1
        assert event_ops[0].name == "audit"
        assert event_ops[0].table == "users"

    def test_add_event_to_existing_table(self) -> None:
        """Adding an event to an existing table should emit DefineEvent."""
        current = self._make_state({"users": TableState(name="users")})
        target = self._make_state(
            {
                "users": TableState(
                    name="users",
                    events={
                        "notify": EventState(
                            name="notify",
                            table="users",
                            when="$event = 'UPDATE'",
                            then="http::post('https://example.com', $after)",
                        )
                    },
                )
            }
        )
        ops = current.diff(target)

        event_ops = [op for op in ops if isinstance(op, DefineEvent)]
        assert len(event_ops) == 1
        assert event_ops[0].name == "notify"

    def test_remove_event(self) -> None:
        """Removing an event should emit RemoveEvent."""
        current = self._make_state(
            {
                "users": TableState(
                    name="users",
                    events={"old_evt": EventState(name="old_evt", table="users", when="true", then="true")},
                )
            }
        )
        target = self._make_state({"users": TableState(name="users")})
        ops = current.diff(target)

        remove_ops = [op for op in ops if isinstance(op, RemoveEvent)]
        assert len(remove_ops) == 1
        assert remove_ops[0].name == "old_evt"

    def test_change_event(self) -> None:
        """Changing an event should emit RemoveEvent + DefineEvent."""
        current = self._make_state(
            {
                "users": TableState(
                    name="users",
                    events={"evt": EventState(name="evt", table="users", when="true", then="action_v1")},
                )
            }
        )
        target = self._make_state(
            {
                "users": TableState(
                    name="users",
                    events={"evt": EventState(name="evt", table="users", when="true", then="action_v2")},
                )
            }
        )
        ops = current.diff(target)

        remove_ops = [op for op in ops if isinstance(op, RemoveEvent)]
        define_ops = [op for op in ops if isinstance(op, DefineEvent)]
        assert len(remove_ops) == 1
        assert len(define_ops) == 1
        assert define_ops[0].then == "action_v2"

    def test_no_change_no_event_ops(self) -> None:
        """Identical events should produce no event operations."""
        evt = EventState(name="evt", table="users", when="true", then="action")
        current = self._make_state({"users": TableState(name="users", events={"evt": evt})})
        target = self._make_state({"users": TableState(name="users", events={"evt": evt})})
        ops = current.diff(target)

        event_ops = [op for op in ops if isinstance(op, (DefineEvent, RemoveEvent))]
        assert len(event_ops) == 0
