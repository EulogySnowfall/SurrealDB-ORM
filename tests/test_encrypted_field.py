"""
Unit tests for Encrypted field type.
"""

from pydantic import BaseModel

from src.surreal_orm.fields.encrypted import (
    Encrypted,
    EncryptedField,
    EncryptedFieldInfo,
    get_encryption_info,
    is_encrypted_field,
)
from src.surreal_orm.types import EncryptionAlgorithm


class TestEncryptedFieldInfo:
    """Tests for EncryptedFieldInfo dataclass."""

    def test_default_algorithm(self) -> None:
        """Test default encryption algorithm is argon2."""
        info = EncryptedFieldInfo()
        assert info.algorithm == EncryptionAlgorithm.ARGON2

    def test_custom_algorithm(self) -> None:
        """Test setting custom encryption algorithm."""
        info = EncryptedFieldInfo(algorithm=EncryptionAlgorithm.BCRYPT)
        assert info.algorithm == EncryptionAlgorithm.BCRYPT

    def test_compare_function(self) -> None:
        """Test compare function generation."""
        info = EncryptedFieldInfo(algorithm=EncryptionAlgorithm.ARGON2)
        assert info.compare_function == "crypto::argon2::compare"

        info_bcrypt = EncryptedFieldInfo(algorithm=EncryptionAlgorithm.BCRYPT)
        assert info_bcrypt.compare_function == "crypto::bcrypt::compare"

    def test_generate_function(self) -> None:
        """Test generate function generation."""
        info = EncryptedFieldInfo(algorithm=EncryptionAlgorithm.ARGON2)
        assert info.generate_function == "crypto::argon2::generate"

        info_scrypt = EncryptedFieldInfo(algorithm=EncryptionAlgorithm.SCRYPT)
        assert info_scrypt.generate_function == "crypto::scrypt::generate"


class TestEncryptedType:
    """Tests for Encrypted type alias."""

    def test_encrypted_in_pydantic_model(self) -> None:
        """Test that Encrypted works in a Pydantic model."""

        class UserModel(BaseModel):
            email: str
            password: Encrypted

        # Should not raise
        user = UserModel(email="test@example.com", password="secret123")
        assert user.email == "test@example.com"
        assert user.password == "secret123"

    def test_encrypted_validates_as_string(self) -> None:
        """Test that Encrypted validates string values."""

        class UserModel(BaseModel):
            password: Encrypted

        # Valid string password
        user = UserModel(password="mypassword")
        assert user.password == "mypassword"

    def test_encrypted_json_schema(self) -> None:
        """Test that Encrypted produces proper JSON schema."""

        class UserModel(BaseModel):
            password: Encrypted

        schema = UserModel.model_json_schema()
        # Password field should be in schema
        assert "password" in schema["properties"]


class TestEncryptedField:
    """Tests for EncryptedField factory function."""

    def test_encrypted_field_default_algorithm(self) -> None:
        """Test EncryptedField with default algorithm."""
        field_type = EncryptedField()
        assert is_encrypted_field(field_type) is True

    def test_encrypted_field_custom_algorithm(self) -> None:
        """Test EncryptedField with custom algorithm."""
        field_type = EncryptedField(EncryptionAlgorithm.BCRYPT)
        info = get_encryption_info(field_type)
        assert info is not None
        assert info.algorithm == EncryptionAlgorithm.BCRYPT

    def test_encrypted_field_in_model(self) -> None:
        """Test EncryptedField in a Pydantic model."""
        BcryptField = EncryptedField(EncryptionAlgorithm.BCRYPT)

        class SecureModel(BaseModel):
            password: BcryptField  # type: ignore

        model = SecureModel(password="secret")
        assert model.password == "secret"


class TestIsEncryptedField:
    """Tests for is_encrypted_field helper function."""

    def test_detects_encrypted_type(self) -> None:
        """Test detection of Encrypted type."""
        assert is_encrypted_field(Encrypted) is True

    def test_detects_encrypted_field(self) -> None:
        """Test detection of EncryptedField type."""
        field_type = EncryptedField()
        assert is_encrypted_field(field_type) is True

    def test_rejects_non_encrypted_type(self) -> None:
        """Test rejection of non-Encrypted types."""
        assert is_encrypted_field(str) is False
        assert is_encrypted_field(int) is False
        assert is_encrypted_field(list) is False

    def test_rejects_none(self) -> None:
        """Test rejection of None type."""
        assert is_encrypted_field(None) is False


class TestGetEncryptionInfo:
    """Tests for get_encryption_info helper function."""

    def test_returns_info_for_encrypted_field(self) -> None:
        """Test that info is returned for Encrypted types."""
        info = get_encryption_info(Encrypted)
        assert info is not None
        assert isinstance(info, EncryptedFieldInfo)

    def test_returns_none_for_non_encrypted(self) -> None:
        """Test that None is returned for non-Encrypted types."""
        assert get_encryption_info(str) is None
        assert get_encryption_info(int) is None

    def test_default_algorithm_in_info(self) -> None:
        """Test that default algorithm is argon2."""
        info = get_encryption_info(Encrypted)
        assert info is not None
        assert info.algorithm == EncryptionAlgorithm.ARGON2

    def test_custom_algorithm_in_info(self) -> None:
        """Test extracting custom algorithm."""
        field_type = EncryptedField(EncryptionAlgorithm.SCRYPT)
        info = get_encryption_info(field_type)
        assert info is not None
        assert info.algorithm == EncryptionAlgorithm.SCRYPT
