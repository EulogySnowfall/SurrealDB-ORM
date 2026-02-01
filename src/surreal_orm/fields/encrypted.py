"""
Encrypted field type for password and sensitive data storage.

This module provides the Encrypted[T] type that automatically generates
the appropriate SurrealDB crypto functions in schema definitions.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, get_args, get_origin

from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema, core_schema

from ..types import EncryptionAlgorithm

if TYPE_CHECKING:
    pass


@dataclass
class EncryptedFieldInfo:
    """
    Metadata for encrypted fields used during schema generation.

    Attributes:
        algorithm: The encryption algorithm to use (default: argon2)
        compare_function: SurrealDB function for password comparison
        generate_function: SurrealDB function for password generation
    """

    algorithm: EncryptionAlgorithm = EncryptionAlgorithm.ARGON2

    @property
    def compare_function(self) -> str:
        """Get the SurrealDB compare function for this algorithm."""
        return f"crypto::{self.algorithm}::compare"

    @property
    def generate_function(self) -> str:
        """Get the SurrealDB generate function for this algorithm."""
        return f"crypto::{self.algorithm}::generate"


class _EncryptedMarker:
    """
    Marker class for encrypted fields.

    This is used internally to mark fields as encrypted in Pydantic's
    type annotation system.
    """

    algorithm: EncryptionAlgorithm

    def __init__(self, algorithm: EncryptionAlgorithm = EncryptionAlgorithm.ARGON2):
        self.algorithm = algorithm

    @classmethod
    def __get_pydantic_core_schema__(
        cls,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        """Build the Pydantic core schema for validation."""
        # Get algorithm from marker if available
        algorithm = EncryptionAlgorithm.ARGON2
        args = get_args(source_type)
        for arg in args:
            if isinstance(arg, _EncryptedMarker):
                algorithm = arg.algorithm
                break

        return core_schema.str_schema(
            metadata={
                "encrypted": True,
                "algorithm": str(algorithm),
                "surreal_type": "string",
                "generate_function": f"crypto::{algorithm}::generate",
                "compare_function": f"crypto::{algorithm}::compare",
            }
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls,
        core_schema_obj: CoreSchema,
        handler: GetJsonSchemaHandler,
    ) -> JsonSchemaValue:
        """Generate JSON schema for OpenAPI documentation."""
        return {"type": "string", "format": "password"}


# Type alias for Encrypted[str] using Annotated
Encrypted = Annotated[str, _EncryptedMarker()]


def EncryptedField(
    algorithm: EncryptionAlgorithm = EncryptionAlgorithm.ARGON2,
) -> Any:
    """
    Create an encrypted field type with a specific algorithm.

    Usage:
        class User(BaseSurrealModel):
            password: EncryptedField(EncryptionAlgorithm.BCRYPT)

    Args:
        algorithm: The encryption algorithm to use

    Returns:
        Annotated type with encryption marker
    """
    return Annotated[str, _EncryptedMarker(algorithm)]


def is_encrypted_field(field_type: Any) -> bool:
    """
    Check if a field type is an Encrypted type.

    Args:
        field_type: The type annotation to check

    Returns:
        True if the field is an Encrypted type
    """
    # Check for Annotated type with _EncryptedMarker
    origin = get_origin(field_type)

    if origin is Annotated:
        args = get_args(field_type)
        for arg in args:
            if isinstance(arg, _EncryptedMarker):
                return True

    # Also check for _EncryptedMarker class directly
    if isinstance(field_type, type) and issubclass(field_type, _EncryptedMarker):
        return True

    return False


def get_encryption_info(field_type: Any) -> EncryptedFieldInfo | None:
    """
    Extract encryption information from a field type.

    Args:
        field_type: The type annotation to extract from

    Returns:
        EncryptedFieldInfo if the field is encrypted, None otherwise
    """
    if not is_encrypted_field(field_type):
        return None

    # Get algorithm from Annotated args
    algorithm = EncryptionAlgorithm.ARGON2
    origin = get_origin(field_type)

    if origin is Annotated:
        args = get_args(field_type)
        for arg in args:
            if isinstance(arg, _EncryptedMarker):
                algorithm = arg.algorithm
                break

    return EncryptedFieldInfo(algorithm=algorithm)
