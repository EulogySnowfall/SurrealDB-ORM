"""
Model introspection for migration generation.

This module extracts schema information from Pydantic models to build
a SchemaState that can be compared against the current database state.
"""

import types
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin, get_type_hints

from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from ..fields.computed import _ComputedMarker, get_computed_expression, is_computed_field
from ..fields.encrypted import is_encrypted_field
from ..fields.references import _ReferencesMarker
from ..fields.relation import _ForeignKeyMarker, _ManyToManyMarker, _RelationMarker
from ..types import PYTHON_TO_SURREAL_TYPE, FieldType, TableType
from .state import AccessState, FieldState, SchemaState, TableState

if TYPE_CHECKING:
    from ..model_base import BaseSurrealModel


#: Markers that carry schema information the plain type annotation cannot.
_RELATION_MARKERS = (_ForeignKeyMarker, _ManyToManyMarker, _RelationMarker, _ReferencesMarker)

#: Markers for fields that exist only in Python.  Graph edges live in their own
#: edge tables, so a ``ManyToMany`` or ``Relation`` attribute has no column to
#: define — their own core schema says so with ``surreal_type: "virtual"``.
_VIRTUAL_MARKERS = (_ManyToManyMarker, _RelationMarker)


def _get_relation_marker(
    type_hint: Any,
    field_info: FieldInfo,
) -> _ForeignKeyMarker | _ManyToManyMarker | _RelationMarker | _ReferencesMarker | None:
    """
    Find the relation or reference marker attached to a field.

    Read from the ``Annotated`` type hint when one is available, and from
    ``FieldInfo.metadata`` otherwise — ``_introspect_model`` falls back to
    ``FieldInfo.annotation`` when ``get_type_hints`` fails, and Pydantic strips
    the ``Annotated`` metadata off that attribute.

    Args:
        type_hint: The field's type annotation
        field_info: Pydantic FieldInfo for the field

    Returns:
        The marker instance, or None when the field carries none
    """
    if get_origin(type_hint) is Annotated:
        for arg in get_args(type_hint):
            if isinstance(arg, _RELATION_MARKERS):
                return arg

    for meta in field_info.metadata:
        if isinstance(meta, _RELATION_MARKERS):
            return meta

    return None


class ModelIntrospector:
    """
    Extracts schema information from Pydantic models.

    This class inspects model definitions including:
    - Table configuration (name, type, schema mode)
    - Field definitions with types and constraints
    - Encrypted field detection
    - Access definition generation for USER tables
    """

    def __init__(self, models: list[type["BaseSurrealModel"]] | None = None):
        """
        Initialize the introspector with a list of models.

        Args:
            models: List of model classes to introspect. If None, uses
                    all registered models.
        """
        if models is None:
            from ..model_base import get_registered_models

            models = get_registered_models()
        self.models = models

    def introspect(self) -> SchemaState:
        """
        Build SchemaState from all registered models.

        Returns:
            SchemaState representing all model definitions
        """
        state = SchemaState()

        for model in self.models:
            table_state = self._introspect_model(model)
            state.tables[table_state.name] = table_state

        return state

    def _introspect_model(self, model: type["BaseSurrealModel"]) -> TableState:
        """
        Extract table state from a single model.

        Args:
            model: The model class to introspect

        Returns:
            TableState representing the model's schema
        """
        # Get table configuration
        table_name = model.get_table_name()
        table_type = model.get_table_type()
        schema_mode = model.get_schema_mode()
        changefeed = model.get_changefeed()
        permissions = model.get_permissions()

        # Build table state
        table_state = TableState(
            name=table_name,
            schema_mode=str(schema_mode),
            table_type=str(table_type),
            changefeed=changefeed,
            permissions=permissions,
        )

        # Introspect fields
        try:
            type_hints = get_type_hints(model, include_extras=True)
        except Exception:
            # Fallback if type hints fail
            type_hints = {}

        for field_name, field_info in model.model_fields.items():
            # Skip the id field - SurrealDB handles it automatically
            if field_name == "id":
                continue

            field_type_hint = type_hints.get(field_name, field_info.annotation)
            field_state = self._introspect_field(field_name, field_type_hint, field_info, model)
            # Virtual fields (ManyToMany, Relation) define no column
            if field_state is None:
                continue
            table_state.fields[field_name] = field_state

        # Generate access definition for USER tables
        if table_type == TableType.USER:
            table_state.access = self._generate_access_state(model, table_name)

        return table_state

    def _introspect_field(
        self,
        name: str,
        type_hint: Any,
        field_info: FieldInfo,
        model: type["BaseSurrealModel"] | None = None,
    ) -> FieldState | None:
        """
        Extract field state from type hint and field info.

        Args:
            name: Field name
            type_hint: Type annotation for the field
            field_info: Pydantic FieldInfo
            model: The model class (used to read ``flexible_fields`` from config)

        Returns:
            FieldState representing the field definition, or None when the
            field is virtual and therefore defines no column
        """
        # Relation markers carry information the annotation alone cannot: a
        # ForeignKey is annotated `str | None` and a ReferencesField
        # `list[str] | None`, so without this both would map to their Python
        # type and lose the record link entirely (#170).
        marker = _get_relation_marker(type_hint, field_info)
        if marker is not None:
            if isinstance(marker, _VIRTUAL_MARKERS):
                return None
            return self._introspect_link_field(name, marker)

        # Check for Computed type
        computed_expression: str | None = None
        if is_computed_field(type_hint):
            computed_expression = get_computed_expression(type_hint)
            # Unwrap Annotated[T | None, _ComputedMarker] to get inner type
            args = get_args(type_hint)
            non_marker = [a for a in args if not isinstance(a, _ComputedMarker)]
            if non_marker:
                type_hint = non_marker[0]  # e.g., str | None
            else:
                type_hint = str  # pragma: no cover

        # Check for Encrypted type
        encrypted = is_encrypted_field(type_hint)

        # Unwrap Encrypted type to get inner type
        if encrypted:
            args = get_args(type_hint)
            if args:
                type_hint = args[0]
            else:
                type_hint = str

        # Handle Optional/Union types
        nullable = False
        origin = get_origin(type_hint)

        if origin is types.UnionType or origin is type(None):
            # Handle X | None syntax
            args = get_args(type_hint)
            non_none_args = [a for a in args if a is not type(None)]
            if type(None) in args:
                nullable = True
            if non_none_args:
                type_hint = non_none_args[0]
            else:
                type_hint = str

        # Map Python type to SurrealDB type
        surreal_type = self._map_type(type_hint)

        # Get default value (skip for computed fields — they have VALUE clause)
        default = None
        if not computed_expression:
            if field_info.default is not None and field_info.default is not ... and field_info.default is not PydanticUndefined:
                default = field_info.default
            elif field_info.default_factory is not None:
                # Can't serialize factory, skip default
                pass

        # Check for flexible type (via json_schema_extra or model_config.flexible_fields)
        flexible = False
        if hasattr(field_info, "json_schema_extra") and field_info.json_schema_extra:
            if isinstance(field_info.json_schema_extra, dict):
                flexible_val = field_info.json_schema_extra.get("flexible", False)
                flexible = flexible_val is True

        if not flexible and model is not None:
            model_config = getattr(model, "model_config", {})
            flexible_fields = model_config.get("flexible_fields") or []
            if name in flexible_fields:
                flexible = True

        return FieldState(
            name=name,
            field_type=surreal_type,
            nullable=nullable,
            default=default,
            encrypted=encrypted,
            flexible=flexible,
            value=computed_expression,
        )

    def _introspect_link_field(
        self,
        name: str,
        marker: _ForeignKeyMarker | _ReferencesMarker,
    ) -> FieldState:
        """
        Build the field state for a record-link field.

        Both :func:`~surreal_orm.fields.ForeignKey` and
        :class:`~surreal_orm.fields.ReferencesField` become a ``record<>``
        column carrying SurrealDB 3.x's ``REFERENCE`` clause; they differ only
        in arity.  Both are optional in Python, so both are nullable.

        Args:
            name: Field name
            marker: The foreign-key or references marker on the field

        Returns:
            FieldState for the record link
        """
        # Local import: model_base imports the relation fields at load time,
        # so importing it at module scope would be circular.
        from ..model_base import _resolve_target_table

        if isinstance(marker, _ForeignKeyMarker):
            target = _resolve_target_table(marker.to)
            # An unresolved target stays an untyped `record` rather than an
            # invented table name — a wrong table would be rejected on write.
            field_type = FieldType.RECORD.generic(target) if target else FieldType.RECORD.value
        else:
            target = marker.table
            field_type = FieldType.ARRAY.generic(FieldType.RECORD.generic(target))

        return FieldState(
            name=name,
            field_type=field_type,
            nullable=True,
            reference=True,
            on_delete=marker.on_delete,
        )

    def _map_type(self, python_type: Any) -> str:
        """
        Map a Python type to a SurrealDB type string.

        Args:
            python_type: Python type annotation

        Returns:
            SurrealDB type string
        """
        origin = get_origin(python_type)

        # Handle generic types
        if origin is list:
            args = get_args(python_type)
            if args:
                inner_type = self._map_type(args[0])
                return f"array<{inner_type}>"
            return "array"

        if origin is dict:
            return "object"

        if origin is set:
            args = get_args(python_type)
            if args:
                inner_type = self._map_type(args[0])
                return f"set<{inner_type}>"
            return "set"

        # Handle basic types
        if python_type in PYTHON_TO_SURREAL_TYPE:
            return PYTHON_TO_SURREAL_TYPE[python_type].value

        # Handle string type names
        _TYPE_MAP = {
            "str": FieldType.STRING.value,
            "int": FieldType.INT.value,
            "float": FieldType.FLOAT.value,
            "bool": FieldType.BOOL.value,
            "datetime": FieldType.DATETIME.value,
            "uuid": FieldType.UUID.value,
            "bytes": FieldType.BYTES.value,
        }

        if isinstance(python_type, type):
            result = _TYPE_MAP.get(python_type.__name__.lower())
            if result is not None:
                return result

        # Fallback
        return FieldType.ANY.value

    def _generate_access_state(
        self,
        model: type["BaseSurrealModel"],
        table_name: str,
    ) -> AccessState:
        """
        Generate access state for a USER type table.

        Args:
            model: The user model class
            table_name: Name of the table

        Returns:
            AccessState for authentication
        """
        identifier_field = model.get_identifier_field()
        password_field = model.get_password_field()

        # Build signup fields
        signup_fields: dict[str, str] = {}

        # Get all model fields (skip computed — they have VALUE clauses)
        computed_fields = getattr(model, "_computed_expressions", {})
        for field_name in model.model_fields:
            if field_name == "id" or field_name in computed_fields:
                continue

            if field_name == password_field:
                # Password gets encrypted
                signup_fields[field_name] = f"crypto::argon2::generate(${field_name})"
            else:
                # Regular fields are passed through
                signup_fields[field_name] = f"${field_name}"

        # Add default timestamp only if the model defines a created_at field
        if "created_at" in model.model_fields and "created_at" not in signup_fields:
            signup_fields["created_at"] = "time::now()"

        # Build signin WHERE clause
        signin_where = (
            f"{identifier_field} = ${identifier_field} AND crypto::argon2::compare({password_field}, ${password_field})"
        )

        # Get duration settings from config
        config = getattr(model, "model_config", {})
        token_duration = config.get("token_duration", "15m")
        session_duration = config.get("session_duration", "12h")

        return AccessState(
            name=f"{table_name.lower()}_auth",
            table=table_name,
            signup_fields=signup_fields,
            signin_where=signin_where,
            duration_token=token_duration,
            duration_session=session_duration,
        )


def introspect_models(
    models: list[type["BaseSurrealModel"]] | None = None,
) -> SchemaState:
    """
    Convenience function to introspect models.

    Args:
        models: List of model classes, or None to use all registered models

    Returns:
        SchemaState representing the models
    """
    introspector = ModelIntrospector(models)
    return introspector.introspect()
