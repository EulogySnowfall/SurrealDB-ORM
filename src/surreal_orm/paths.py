"""
``FieldPath``: a validated builder of SurrealQL idioms over a record's fields.

A ``FieldPath`` describes a value reached from a record — nested fields,
element filters (``[WHERE …]``), indexes (``[0]``, ``[$]``, ``[n]``) and
destructuring (``.{a, b, k: x}``) — and is used as an ``annotate()`` value::

    from surreal_orm import FieldPath, Q

    orders = await Order.objects().annotate(
        big_items=FieldPath("items").where(qty__gt=1).pick("sku", total="price"),
        city=FieldPath("customer.address.city"),
        first_sku=FieldPath("items").field("sku").first(),
    ).exec()

This module holds the data and its validation only. Every argument is checked
at the call: field names and pick keys must be identifiers (or dotted paths of
identifiers), indexes plain non-negative ``int``s, and conditions ``Q``
objects whose values are bound when the path is rendered. Rendering lives in
``QuerySet``, which owns the bound variables of the query (and importing it
here would create an import cycle).
"""

from __future__ import annotations

import functools
import operator
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from surreal_sdk.utils import require_int

from .q import Q, iter_q_leaves, q_has_condition
from .subquery import Subquery
from .utils import validate_field_path, validate_identifier

__all__ = ["FieldPath"]


@dataclass(frozen=True, slots=True)
class _FieldStep:
    """``.name`` — a field (or dotted path) of the current value."""

    name: str


@dataclass(frozen=True, slots=True)
class _WhereStep:
    """``.?[WHERE …]`` — the elements of the current value matching ``condition``."""

    condition: Q


@dataclass(frozen=True, slots=True)
class _IndexStep:
    """``[n]`` / ``[$]`` — one element of the current value."""

    index: int | Literal["$"]


@dataclass(frozen=True, slots=True)
class _PickStep:
    """``.{…}`` — destructuring; ``entries`` are ``(output key, source)`` pairs."""

    entries: tuple[tuple[str, str | FieldPath], ...]


_Step = _FieldStep | _WhereStep | _IndexStep | _PickStep


def _repr_step(step: _Step) -> str:
    if isinstance(step, _FieldStep):
        return f".field({step.name!r})"
    if isinstance(step, _WhereStep):
        return f".where({step.condition!r})"
    if isinstance(step, _IndexStep):
        if step.index == "$":
            return ".last()"
        if step.index == 0:
            return ".first()"
        return f".at({step.index})"
    parts = [repr(key) if key == source else f"{key}={source!r}" for key, source in step.entries]
    return f".pick({', '.join(parts)})"


class FieldPath:
    """An immutable SurrealQL idiom over a record's fields: nested fields, element filters, indexes, destructuring.

    Built step by step; every method returns a new FieldPath and validates its arguments at the call.
    Every step applies to the value built so far: ``FieldPath("items").field("sku").first()`` is the
    first sku, not the first character of each sku.
    A FieldPath is rendered by the QuerySet that uses it (values bound as parameters), never by itself.
    Missing, NONE and NULL values propagate: the path then yields None (optional chaining).

    Args:
        field: A field name or a dotted path of field names (``"customer.address.city"``).

    Raises:
        ValueError: If ``field`` is not a field name or a dotted path of field names.
    """

    __slots__ = ("_root", "_steps")

    _root: str
    _steps: tuple[_Step, ...]

    def __init__(self, field: str) -> None:
        validate_field_path(field, "FieldPath field")
        object.__setattr__(self, "_root", field)
        object.__setattr__(self, "_steps", ())

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"FieldPath is immutable; cannot set {name!r}.")

    def _then(self, step: _Step) -> FieldPath:
        """Return a new path: this one followed by ``step``."""
        new = object.__new__(FieldPath)
        object.__setattr__(new, "_root", self._root)
        object.__setattr__(new, "_steps", (*self._steps, step))
        return new

    # -- steps ---------------------------------------------------------------

    def field(self, name: str) -> FieldPath:
        """``.name`` — a field of the current value (a dotted path is allowed).

        On an array, the result is the list of each element's field; the next
        step applies to that list.

        Raises:
            ValueError: If ``name`` is not a field name or a dotted path of field names.
        """
        validate_field_path(name, "FieldPath field")
        return self._then(_FieldStep(name))

    def where(self, *conditions: Q, **lookups: Any) -> FieldPath:
        """``.?[WHERE …]`` — keep the elements of the current value matching every condition.

        Positional ``Q`` objects and keyword lookups are AND-ed, positional
        first. Keys are relative to the element (``qty`` is the element's
        ``qty``); ``this`` is the element itself. To filter an array *inside
        each element*, use ``pick(key=FieldPath("key").where(...))``.

        Raises:
            TypeError: If a positional argument is not a ``Q``, or a
                condition holds a ``Subquery`` value.
            ValueError: If there is no condition, a key is not a valid filter
                key, or the ``where()`` would follow a ``field()`` step that
                follows an earlier ``where()`` (the server would then test each
                kept element's whole value instead of filtering inside it).
        """
        for arg in conditions:
            if not isinstance(arg, Q):
                raise TypeError(f"FieldPath.where() positional arguments must be Q objects, got {type(arg).__name__!r}.")
        parts: list[Q] = list(conditions)
        if lookups:
            parts.append(Q(**lookups))
        condition = functools.reduce(operator.and_, parts) if parts else Q()
        if not q_has_condition(condition):
            raise ValueError("FieldPath.where() needs at least one condition.")
        for field_name, lookup_name, value in iter_q_leaves(condition):
            if isinstance(value, Subquery):
                raise TypeError(
                    f"A Subquery cannot be used inside an element condition ({f'{field_name}__{lookup_name}'!r}): "
                    "element conditions are evaluated per array element."
                )
        self._check_where_after_field()
        return self._then(_WhereStep(condition))

    def _check_where_after_field(self) -> None:
        """Refuse a ``where()`` on the per-element values of an earlier ``where()`` (E17).

        After ``where()``, a ``field()`` step reads the field of each kept
        element; a ``where()`` on that list tests each element's whole value,
        and an array value comes back unfiltered. An index step in between
        selects one element, which makes the shape correct again.
        """
        if not self._steps or not isinstance(self._steps[-1], _FieldStep):
            return
        name = self._steps[-1].name
        for step in reversed(self._steps[:-1]):
            if isinstance(step, _IndexStep):
                return
            if isinstance(step, _WhereStep):
                raise ValueError(
                    f"FieldPath.where() cannot follow .field({name!r}) after an earlier where(): it would test each "
                    f"kept element's whole {name!r} value instead of filtering inside it. Put the condition in the "
                    f"earlier where(), or filter inside each element with pick(<key>=FieldPath({name!r}).where(...))."
                )

    def first(self) -> FieldPath:
        """``[0]`` — the first element of the current value."""
        return self._then(_IndexStep(0))

    def last(self) -> FieldPath:
        """``[$]`` — the last element of the current value."""
        return self._then(_IndexStep("$"))

    def at(self, index: int) -> FieldPath:
        """``[index]`` — one element of the current value.

        Raises:
            TypeError: If ``index`` is not an ``int`` (a ``bool`` included).
            ValueError: If ``index`` is negative.
        """
        return self._then(_IndexStep(require_int("FieldPath.at() index", index, minimum=0)))

    def pick(self, *fields: str, **aliased: str | FieldPath) -> FieldPath:
        """``.{a, b, key: source}`` — destructuring.

        Positional entries are plain identifiers; an aliased value is a dotted
        path string or a ``FieldPath`` read relative to the object. On an
        array, applies to each element; on a record link, to the linked record.

        Raises:
            ValueError: If no field is given, a name or key is not an
                identifier, a source string is not a field path, or an output
                key is given twice.
            TypeError: If an aliased value is neither a string nor a ``FieldPath``.
        """
        if not fields and not aliased:
            raise ValueError("FieldPath.pick() needs at least one field.")
        entries: list[tuple[str, str | FieldPath]] = []
        seen: set[str] = set()

        def add(key: str, source: str | FieldPath) -> None:
            if key in seen:
                raise ValueError(f"FieldPath.pick() key {key!r} is given twice.")
            seen.add(key)
            entries.append((key, source))

        for name in fields:
            validate_identifier(name, "FieldPath.pick() field")
            add(name, name)
        for key, value in aliased.items():
            validate_identifier(key, "FieldPath.pick() key")
            if isinstance(value, str):
                validate_field_path(value, "FieldPath.pick() source")
            elif not isinstance(value, FieldPath):
                raise TypeError(
                    f"FieldPath.pick() value for {key!r} must be a field path string or a FieldPath, "
                    f"got {type(value).__name__!r}."
                )
            add(key, value)
        return self._then(_PickStep(tuple(entries)))

    # -- introspection -------------------------------------------------------

    def iter_where_conditions(self) -> Iterator[Q]:
        """Yield every element condition of the path, ``pick()`` values included (internal)."""
        for step in self._steps:
            if isinstance(step, _WhereStep):
                yield step.condition
            elif isinstance(step, _PickStep):
                for _, source in step.entries:
                    if isinstance(source, FieldPath):
                        yield from source.iter_where_conditions()

    # -- dunder --------------------------------------------------------------

    def __repr__(self) -> str:
        return f"FieldPath({self._root!r})" + "".join(_repr_step(step) for step in self._steps)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FieldPath):
            return NotImplemented
        return self._root == other._root and self._steps == other._steps

    __hash__ = None  # type: ignore[assignment]
