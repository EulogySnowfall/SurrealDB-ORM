"""
Access definition generator for SurrealDB authentication.

Generates DEFINE ACCESS ... TYPE RECORD statements for user authentication
using SurrealDB's built-in JWT support.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..types import EncryptionAlgorithm, TableType

if TYPE_CHECKING:
    from ..model_base import BaseSurrealModel


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
        signup_fields: Dict of field -> expression for signup
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

        return f"""DEFINE ACCESS {self.name} ON DATABASE TYPE RECORD
    SIGNUP (CREATE {self.table} SET {signup_sets})
    SIGNIN (SELECT * FROM {self.table} WHERE {self.signin_where})
    DURATION FOR TOKEN {self.duration_token}, FOR SESSION {self.duration_session};"""

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

        Only generates for USER type tables.

        Args:
            model: The model class to generate access for

        Returns:
            AccessDefinition if model is USER type, None otherwise
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

        # Build signup fields from model fields
        signup_fields: dict[str, str] = {}

        for field_name in model.model_fields:
            if field_name == "id":
                continue

            if field_name == password_field:
                # Password gets encrypted
                signup_fields[field_name] = f"crypto::{algorithm}::generate(${field_name})"
            else:
                # Regular fields are passed through
                signup_fields[field_name] = f"${field_name}"

        # Add created_at if not in model
        if "created_at" not in signup_fields:
            signup_fields["created_at"] = "time::now()"

        # Get durations from config
        token_duration = config.get("token_duration", "15m")
        session_duration = config.get("session_duration", "12h")

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
