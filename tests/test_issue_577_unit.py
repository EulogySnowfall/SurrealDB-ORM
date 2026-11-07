"""Unit tests for #577 (v2): follow-ups from the 0.21.54 reviews.

* ``ChangeModelStream.stop()`` called before the first ``__anext__()`` set
  ``_stopped``, but ``_stream_events()`` only read that flag after a poll
  failed, so the first iteration still resolved a client and polled as if
  ``stop()`` had never been called. The stream now ends at once.
* ``references`` was missing from the DEFINE parser's parameterised types, so
  a ``references<a | comment>`` union was cut at the clause keyword inside its
  brackets, while ``record<user | comment>`` was read as one type.

No server is involved: the change-feed half reuses the in-memory feed of the
#569 tests.
"""

from __future__ import annotations

import pytest

from src.surreal_orm.migrations.define_parser import _CAST_TYPES, _GENERIC_TYPES, parse_define_field
from tests.test_issue_569_unit import I569Item, I569Server, _next, isolated_registry, server

__all__ = ["isolated_registry", "server"]


async def test_changes_stopped_before_the_first_iteration_end_without_polling(server: I569Server) -> None:
    """stop() before the first __anext__() ends the iteration: no client is resolved, no poll is sent."""
    server.feed.write("i569_items", {"id": "i569_items:a"})
    stream = I569Item.objects().changes(since=0, poll_interval=0.01)
    stream.stop()

    with pytest.raises(StopAsyncIteration):
        await _next(stream)
    with pytest.raises(StopAsyncIteration):
        await _next(stream)

    assert server.built == [], "a stopped stream resolved an HTTP client"
    assert server.feed.queries == [], "a stopped stream polled the change feed"
    assert stream._connection is None
    assert stream.cursor is None


def test_references_is_a_parameterised_type() -> None:
    assert "references" in _GENERIC_TYPES
    assert "references" in _CAST_TYPES


def test_references_type_with_keyword_named_table_is_one_bracket() -> None:
    # Mirrors test_type_with_keyword_named_table_is_one_bracket (#568) for references<...>.
    # It passed before the #577 fix too: a two-member union whose last member is
    # keyword-named was already read whole. The regression guard is the
    # parametrised test below, where another member follows the keyword-named one.
    field = parse_define_field("DEFINE FIELD r ON fu TYPE references<a | comment> COMMENT 'c' PERMISSIONS FULL")
    assert field.field_type == "references<a | comment>"
    assert field.comment == "c"


@pytest.mark.parametrize(
    "field_type",
    [
        "references<a | comment | b>",
        "references<a | comment | permissions>",
        "references<user | comment | default>",
    ],
)
def test_references_union_with_keyword_named_tables_is_one_type(field_type: str) -> None:
    # Before the fix the type was cut at the first keyword-named table followed by another member.
    field = parse_define_field(f"DEFINE FIELD r ON fu TYPE {field_type} COMMENT 'c' PERMISSIONS FULL")
    assert field.field_type == field_type
    assert field.comment == "c"
