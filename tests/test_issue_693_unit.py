"""Unit tests for #693: inlining a NamedTuple value in SurrealQL queries.

_extract_datetime_values() converts NamedTuples to plain tuples before json.dumps()
runs, avoiding TypeError when fields have no default and silent corruption when they do.
"""

from datetime import UTC, datetime
from typing import NamedTuple

from src.surreal_orm.utils import _extract_datetime_values, inline_dict_variables


class Coord(NamedTuple):
    lat: float
    lon: float


class Settings(NamedTuple):
    theme: str = "dark"
    timeout: int = 30


class Timestamped(NamedTuple):
    tag: str
    at: datetime


class CustomTuple(tuple):
    pass


class TestExtractDatetimeValuesNamedTuple:
    """Test _extract_datetime_values handling of NamedTuples."""

    def test_namedtuple_without_defaults_top_level(self) -> None:
        pt = Coord(51.5, -0.1)
        res = _extract_datetime_values(pt, {}, [0])
        assert res == (51.5, -0.1)
        assert type(res) is tuple

    def test_namedtuple_with_defaults_top_level(self) -> None:
        cfg = Settings(theme="light", timeout=60)
        res = _extract_datetime_values(cfg, {}, [0])
        assert res == ("light", 60)
        assert type(res) is tuple

    def test_namedtuple_nested_in_dict_and_list(self) -> None:
        pt = Coord(1.0, 2.0)
        cfg = Settings()
        data = {"coord": pt, "items": [cfg, pt]}
        res = _extract_datetime_values(data, {}, [0])
        assert res == {"coord": (1.0, 2.0), "items": [("dark", 30), (1.0, 2.0)]}
        assert type(res["coord"]) is tuple
        assert type(res["items"][0]) is tuple

    def test_namedtuple_with_datetime(self) -> None:
        dt = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        ts = Timestamped("event", dt)
        markers: dict[str, str] = {}
        res = _extract_datetime_values(ts, markers, [0])
        assert res == ("event", "__SURQL_DT_0__")
        assert markers == {"__SURQL_DT_0__": 'd"2026-03-01T12:00:00+00:00"'}

    def test_plain_tuple_and_list_unchanged(self) -> None:
        plain_t = (1, "a", True)
        assert _extract_datetime_values(plain_t, {}, [0]) == plain_t
        assert type(_extract_datetime_values(plain_t, {}, [0])) is tuple

        plain_l = [1, "a", True]
        assert _extract_datetime_values(plain_l, {}, [0]) == plain_l
        assert type(_extract_datetime_values(plain_l, {}, [0])) is list

        custom = CustomTuple([1, 2])
        res = _extract_datetime_values(custom, {}, [0])
        assert type(res) is CustomTuple
        assert res == custom


class TestInlineDictVariablesNamedTuple:
    """Test inline_dict_variables handling of NamedTuples."""

    def test_inline_namedtuple_without_defaults(self) -> None:
        pt = Coord(51.5, -0.1)
        query = "UPDATE place:1 SET location = $data;"
        variables = {"data": {"nested": {"coord": pt}}}
        new_query, remaining = inline_dict_variables(query, variables)
        assert remaining == {}
        assert '{"nested": {"coord": [51.5, -0.1]}}' in new_query

    def test_inline_namedtuple_with_defaults(self) -> None:
        cfg = Settings(theme="light", timeout=10)
        query = "UPDATE app:1 SET config = $data;"
        variables = {"data": {"nested": [cfg]}}
        new_query, remaining = inline_dict_variables(query, variables)
        assert remaining == {}
        assert '{"nested": [["light", 10]]}' in new_query

    def test_inline_namedtuple_with_datetime(self) -> None:
        dt = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
        ts = Timestamped("login", dt)
        query = "CREATE log:1 CONTENT $data;"
        variables = {"data": {"entry": {"details": ts}}}
        new_query, remaining = inline_dict_variables(query, variables)
        assert remaining == {}
        assert 'd"2026-01-02T03:04:00+00:00"' in new_query
        assert '["login", d"2026-01-02T03:04:00+00:00"]' in new_query
