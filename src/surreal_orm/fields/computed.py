"""
Computed field type for server-side SurrealDB expressions.

This module provides the Computed type that generates VALUE clauses
in DEFINE FIELD statements, allowing SurrealDB to automatically
compute field values using expressions like string::concat(),
math::sum(), array::len(), etc.

Usage:
    class User(BaseSurrealModel):
        first_name: str
        last_name: str
        full_name: Computed[str] = Computed("string::concat(first_name, ' ', last_name)")

    class Order(BaseSurrealModel):
        items: list[dict]
        subtotal: Computed[float] = Computed("math::sum(items.*.price * items.*.qty)")
"""

from typing import Annotated, Any, get_args, get_origin

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema


class _ComputedMarker:
    """
    Pydantic-compatible marker for computed fields.

    Stored inside ``Annotated[T | None, _ComputedMarker(T)]`` to carry
    the inner type and (after ``__init_subclass__`` runs) the SurrealQL
    expression.
    """

    inner_type: type
    expression: str

    def __init__(self, inner_type: type = str) -> None:
        self.inner_type = inner_type
        self.expression = ""  # Populated by BaseSurrealModel.__init_subclass__

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build a nullable schema with a default of None."""
        inner_schema = handler.generate_schema(self.inner_type)
        return core_schema.with_default_schema(
            core_schema.nullable_schema(inner_schema),
            default=None,
        )

    def __get_pydantic_json_schema__(
        self,
        _schema: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Generate JSON schema matching the nullable inner type and default."""
        json_schema = handler(_schema)
        if isinstance(json_schema, dict) and "default" not in json_schema:
            json_schema["default"] = None
        return json_schema


class _ComputedDefault:
    """
    Sentinel default value for computed fields.

    Created by ``Computed("expression")`` and replaced with ``None`` by
    ``BaseSurrealModel.__init_subclass__`` before Pydantic processes the class.
    """

    __slots__ = ("expression",)

    def __init__(self, expression: str) -> None:
        self.expression = expression

    def __repr__(self) -> str:
        return f"Computed({self.expression!r})"


class Computed:
    """
    Computed field for server-side SurrealDB expressions.

    Dual-use class:

    - ``Computed[T]`` — type annotation that resolves to
      ``Annotated[T | None, _ComputedMarker(T)]``
    - ``Computed("expression")`` — default value that creates a
      :class:`_ComputedDefault` sentinel

    Example::

        class User(BaseSurrealModel):
            first_name: str
            last_name: str
            full_name: Computed[str] = Computed(
                "string::concat(first_name, ' ', last_name)"
            )

        class Order(BaseSurrealModel):
            items: list[dict]
            discount: float = 0.0
            subtotal: Computed[float] = Computed(
                "math::sum(items.*.price * items.*.qty)"
            )
            total: Computed[float] = Computed("subtotal * (1 - discount)")
            item_count: Computed[int] = Computed("array::len(items)")
    """

    def __class_getitem__(cls, inner_type: type) -> type:
        """``Computed[str]`` → ``Annotated[str | None, _ComputedMarker(str)]``."""
        return Annotated[inner_type | None, _ComputedMarker(inner_type)]  # type: ignore[return-value]

    def __new__(cls, expression: str) -> "_ComputedDefault":  # type: ignore[misc]
        """``Computed("expr")`` → ``_ComputedDefault("expr")``."""
        return _ComputedDefault(expression)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _get_computed_marker(annotation: Any) -> _ComputedMarker | None:
    """
    Extract the ``_ComputedMarker`` instance from a type annotation.

    Works with ``Annotated[T | None, _ComputedMarker(...)]``.
    """
    origin = get_origin(annotation)
    if origin is Annotated:
        for arg in get_args(annotation):
            if isinstance(arg, _ComputedMarker):
                return arg
    return None


def is_computed_field(field_type: Any) -> bool:
    """
    Check if a field type is a Computed type.

    Args:
        field_type: The type annotation to check

    Returns:
        True if the field has a ``_ComputedMarker``
    """
    return _get_computed_marker(field_type) is not None


def get_computed_expression(field_type: Any) -> str | None:
    """
    Extract the SurrealQL expression from a computed field type.

    Args:
        field_type: The type annotation to extract from

    Returns:
        The expression string if the field is computed, None otherwise
    """
    marker = _get_computed_marker(field_type)
    if marker is not None and marker.expression:
        return marker.expression
    return None
