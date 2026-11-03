"""Issue #492 — a compact ``Duration`` compares equal whatever sequence built it.

``Duration`` is a dataclass, so equality compares ``value`` as is. A duration
read from the server holds a list, so one built from a tuple (a form the
docstring allows) compared unequal to it, although both encode to the same
tag 14 payload and render the same ``str()``. ``__post_init__`` stores a
compact value as a list; a string value is left as it is.
"""

import cbor2
import pytest
from cbor2 import CBORTag

from src.surreal_sdk.protocol.cbor import Duration, decode, encode


class TestDurationEquality:
    @pytest.mark.parametrize(
        ("as_tuple", "as_list"),
        [
            ((5400, 1), [5400, 1]),
            ((5400,), [5400]),
            ((), []),
        ],
    )
    def test_tuple_equals_list(self, as_tuple: tuple[int, ...], as_list: list[int]) -> None:
        assert Duration(as_tuple) == Duration(as_list)

    def test_tuple_equals_decoded_server_value(self) -> None:
        assert Duration((5400, 1)) == decode(cbor2.dumps(CBORTag(14, [5400, 1])))

    @pytest.mark.parametrize("value", [(5400, 1), (5400,), ()])
    def test_tuple_is_stored_as_list(self, value: tuple[int, ...]) -> None:
        duration = Duration(value)
        assert isinstance(duration.value, list)
        assert duration.value == list(value)

    def test_list_is_copied(self) -> None:
        source = [5400, 1]
        duration = Duration(source)
        source.append(7)
        assert duration.value == [5400, 1]

    def test_string_is_left_untouched(self) -> None:
        duration = Duration("1h30m")
        assert duration.value == "1h30m"
        assert isinstance(duration.value, str)
        assert duration != Duration(list("1h30m"))

    @pytest.mark.parametrize(
        ("value", "text"),
        [((5400, 1), "5400s1ns"), ((5400,), "5400s"), ((), "0ns"), ("1h30m", "1h30m")],
    )
    def test_str_unchanged(self, value: str | tuple[int, ...], text: str) -> None:
        assert str(Duration(value)) == text

    @pytest.mark.parametrize("value", [(5400, 1), (5400,), ()])
    def test_tuple_encodes_as_tag_14_and_round_trips(self, value: tuple[int, ...]) -> None:
        raw = cbor2.loads(encode(Duration(value)))
        assert raw == CBORTag(14, list(value))
        assert decode(encode(Duration(value))) == Duration(value)
