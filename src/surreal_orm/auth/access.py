"""
Access definition generator for SurrealDB authentication.

Generates DEFINE ACCESS ... TYPE RECORD statements for user authentication
using SurrealDB's built-in JWT support.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..types import EncryptionAlgorithm, TableType

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

    from ..model_base import BaseSurrealModel


def _is_read_only(field_info: "FieldInfo") -> bool:
    """
    Tell whether a model field is declared read-only.

    A field is read-only when Pydantic freezes it (``Field(frozen=True)``) or
    when it carries ``readonly`` in ``json_schema_extra`` — the form
    ``inspectdb`` generates for a ``READONLY`` column.
    """
    if field_info.frozen:
        return True
    extra = field_info.json_schema_extra
    return isinstance(extra, dict) and extra.get("readonly") is True


def get_signup_field_names(model: type["BaseSurrealModel"]) -> list[str]:
    """
    Return the validated ``signup_fields`` entries of a USER model.

    ``signup_fields`` lists the fields a caller may set at sign-up, besides
    the identifier and the password, which always belong to the sign-up set.

    Args:
        model: The model class to read ``signup_fields`` from

    Returns:
        The listed field names, in configuration order, without duplicates.

    Raises:
        ValueError: If ``signup_fields`` is not a list of names, or an entry
            is ``id``, the identifier, the password, ``created_at``, a name
            that is not a model field, a computed field, a ``server_fields``
            entry or a read-only field.  The message names the field and
            the reason.
    """
    config: Any = getattr(model, "model_config", {})
    raw = config.get("signup_fields") or []
    model_name = model.__name__
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise ValueError(f"signup_fields on {model_name} must be a list of field names, got {raw!r}.")

    identifier = model.get_identifier_field()
    password = model.get_password_field()
    server_fields = set(config.get("server_fields") or [])
    computed = getattr(model, "_computed_expressions", None) or {}
    model_fields = model.model_fields

    names: list[str] = []
    for name in raw:
        if not isinstance(name, str):
            raise ValueError(f"signup_fields on {model_name} must be a list of field names, got entry {name!r}.")
        reason: str | None = None
        if name == "id":
            reason = "the record ID is assigned by the server"
        elif name == identifier:
            reason = "it is the identifier field, which is always part of the sign-up set"
        elif name == password:
            reason = "it is the password field, which is always part of the sign-up set"
        elif name not in model_fields:
            reason = f"it is not a field of {model_name}"
        elif name in computed:
            reason = "it is a computed field, whose value comes from its VALUE clause"
        elif name in server_fields:
            reason = "it is listed in server_fields, so the server sets its value"
        elif name == "created_at":
            reason = "it is set to time::now() at sign-up"
        elif _is_read_only(model_fields[name]):
            reason = "it is a read-only field"
        if reason is not None:
            raise ValueError(f"signup_fields entry {name!r} on {model_name} is not allowed: {reason}.")
        if name not in names:
            names.append(name)
    return names


def get_signup_parameters(model: type["BaseSurrealModel"]) -> set[str]:
    """
    Return the parameter names a sign-up request may carry for a model.

    These are the identifier, the password and the ``signup_fields``
    entries; :meth:`AuthenticatedUserMixin.signup` rejects any other keyword.

    Raises:
        ValueError: If ``signup_fields`` is invalid (see
            :func:`get_signup_field_names`).
    """
    return {model.get_identifier_field(), model.get_password_field(), *get_signup_field_names(model)}


def build_signup_fields(
    model: type["BaseSurrealModel"],
    algorithm: EncryptionAlgorithm | str = EncryptionAlgorithm.ARGON2,
) -> dict[str, str]:
    """
    Build the ``SET`` assignments of a USER model's ``SIGNUP`` clause.

    The single source of the sign-up set, shared by
    :meth:`AccessGenerator.from_model` (``define_access()``) and the
    migration introspector (``makemigrations`` / ``schema_diff``), so the two
    always emit the same clause.

    Only these fields are assigned:

    - the identifier, as ``$<identifier>``;
    - the password, as ``crypto::<algorithm>::generate($<password>)``;
    - each ``signup_fields`` entry, as ``$<field>``;
    - ``created_at``, as ``time::now()``, when the model has that field.

    Every other field is left out of the clause, so the field's schema
    ``DEFAULT`` (emitted from the model default) or, on a schemaless table,
    the Pydantic default on load supplies its value.

    Args:
        model: The USER model class
        algorithm: Password hashing algorithm

    Returns:
        Ordered mapping of field name to SurrealQL expression.

    Raises:
        ValueError: If ``signup_fields`` is invalid (see
            :func:`get_signup_field_names`).
    """
    identifier = model.get_identifier_field()
    password = model.get_password_field()
    listed = set(get_signup_field_names(model))
    computed = getattr(model, "_computed_expressions", None) or {}

    signup_fields: dict[str, str] = {
        identifier: f"${identifier}",
        password: f"crypto::{algorithm}::generate(${password})",
    }
    # Declaration order keeps the emitted clause stable across runs.
    for field_name in model.model_fields:
        if field_name in listed:
            signup_fields[field_name] = f"${field_name}"
    # created_at is stamped by the server, never taken from the caller.
    if "created_at" in model.model_fields and "created_at" not in computed:
        signup_fields["created_at"] = "time::now()"
    return signup_fields


@dataclass
class AccessDefinition:
    """
    Configuration for generating DEFINE ACCESS statements.

    This class represents the authentication configuration for a USER table,
    including signup/signin logic and token durations.

    Attributes:
        name: Access definition name (e.g., "user_auth")
        table: Associated table name
        identifier_field: Field used for signin (e.g., "email")
        password_field: Field containing password
        signup_fields: Dict of field -> expression for the SIGNUP clause
        signin_where: WHERE clause for signin validation
        duration_token: JWT token lifetime (e.g., "15m")
        duration_session: Session lifetime (e.g., "12h")
        algorithm: Password hashing algorithm
    """

    name: str
    table: str
    identifier_field: str = "email"
    password_field: str = "password"
    signup_fields: dict[str, str] = field(default_factory=dict)
    signin_where: str | None = None
    duration_token: str = "15m"
    duration_session: str = "12h"
    algorithm: EncryptionAlgorithm = EncryptionAlgorithm.ARGON2
    with_refresh: bool = False
    duration_grant: str = "30d"

    def __post_init__(self) -> None:
        """Build default signin_where if not provided."""
        if not self.signin_where:
            self.signin_where = (
                f"{self.identifier_field} = ${self.identifier_field} AND "
                f"crypto::{self.algorithm}::compare({self.password_field}, ${self.password_field})"
            )

    def to_surreal_ql(self) -> str:
        """
        Generate the DEFINE ACCESS SurrealQL statement.

        Returns:
            Complete DEFINE ACCESS statement
        """
        # Build signup SET clause
        signup_sets = ", ".join(f"{field_name} = {expr}" for field_name, expr in self.signup_fields.items())

        parts = [
            f"DEFINE ACCESS {self.name} ON DATABASE TYPE RECORD",
            f"    SIGNUP (CREATE {self.table} SET {signup_sets})",
            f"    SIGNIN (SELECT * FROM {self.table} WHERE {self.signin_where})",
        ]

        if self.with_refresh:
            parts.append("    WITH REFRESH")

        duration = f"DURATION FOR TOKEN {self.duration_token}, FOR SESSION {self.duration_session}"
        if self.with_refresh:
            duration += f", FOR GRANT {self.duration_grant}"
        parts.append(f"    {duration};")

        return "\n".join(parts)

    def to_remove_ql(self) -> str:
        """
        Generate the REMOVE ACCESS SurrealQL statement.

        Returns:
            REMOVE ACCESS statement
        """
        return f"REMOVE ACCESS {self.name} ON DATABASE;"


class AccessGenerator:
    """
    Generates AccessDefinition objects from User models.

    This class inspects USER type models and generates the appropriate
    DEFINE ACCESS configuration for authentication.
    """

    @staticmethod
    def from_model(model: type["BaseSurrealModel"]) -> AccessDefinition | None:
        """
        Generate AccessDefinition from a model.

        Only generates for USER type tables.  The ``SIGNUP`` clause assigns
        the identifier, the password, the ``signup_fields`` entries and
        ``created_at`` (see :func:`build_signup_fields`); every other field
        keeps its schema or model default.

        Args:
            model: The model class to generate access for

        Returns:
            AccessDefinition if model is USER type, None otherwise

        Raises:
            ValueError: If ``signup_fields`` is invalid.
        """
        # Check if this is a USER table
        table_type = model.get_table_type()
        if table_type != TableType.USER:
            return None

        table_name = model.get_table_name()
        identifier_field = model.get_identifier_field()
        password_field = model.get_password_field()

        # Get algorithm from config or default
        config = getattr(model, "model_config", {})
        algorithm_str = config.get("encryption_algorithm", "argon2")
        try:
            algorithm = EncryptionAlgorithm(algorithm_str)
        except ValueError:
            algorithm = EncryptionAlgorithm.ARGON2

        signup_fields = build_signup_fields(model, algorithm)

        # Get durations from config
        token_duration = config.get("token_duration", "15m")
        session_duration = config.get("session_duration", "12h")
        with_refresh = config.get("with_refresh", False) or False
        grant_duration = config.get("grant_duration", "30d")

        access_name = config.get("access_name") or f"{table_name.lower()}_auth"

        return AccessDefinition(
            name=access_name,
            table=table_name,
            identifier_field=identifier_field,
            password_field=password_field,
            signup_fields=signup_fields,
            duration_token=token_duration,
            duration_session=session_duration,
            algorithm=algorithm,
            with_refresh=with_refresh,
            duration_grant=grant_duration,
        )

    @staticmethod
    def generate_all(models: list[type["BaseSurrealModel"]]) -> list[AccessDefinition]:
        """
        Generate AccessDefinitions for all USER type models.

        Args:
            models: List of model classes

        Returns:
            List of AccessDefinition objects for USER tables
        """
        definitions = []
        for model in models:
            definition = AccessGenerator.from_model(model)
            if definition:
                definitions.append(definition)
        return definitions
