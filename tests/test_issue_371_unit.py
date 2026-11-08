"""
Unit tests for issue #371: a required field outside the sign-up set is reported
when the access definition is built.

The ``SIGNUP`` clause assigns only the identifier, the password, the
``signup_fields`` entries and ``created_at`` (#333).  A required model field
outside that set can never be set by a sign-up, so the first sign-up used to be
the first place the mistake showed up.  ``build_signup_fields()`` now emits one
``UserWarning`` naming those fields; the clause it returns is unchanged.
"""

import warnings
from datetime import datetime

import pytest

from src.surreal_orm.auth.access import AccessGenerator, build_signup_fields
from src.surreal_orm.fields import Computed, Encrypted, ManyToMany, Relation
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict, clear_model_registry
from src.surreal_orm.types import TableType


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear the model registry before each test."""
    clear_model_registry()


def _warnings_of(model: type[BaseSurrealModel]) -> tuple[dict[str, str], list[warnings.WarningMessage]]:
    """Build the sign-up set of ``model`` and return it with every warning emitted."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fields = build_signup_fields(model)
    return fields, list(caught)


class TestRequiredFieldWarning:
    """A required field the sign-up cannot set is named in one UserWarning."""

    def test_warning_names_the_field(self) -> None:
        class I371Named(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i371_named")
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        fields, caught = _warnings_of(I371Named)

        assert len(caught) == 1
        assert caught[0].category is UserWarning
        message = str(caught[0].message)
        assert "I371Named" in message
        assert "'name'" in message
        assert "signup_fields" in message
        assert "server_fields" in message
        # The warning reports; the emitted clause is the same as before.
        assert fields == {"email": "$email", "password": "crypto::argon2::generate($password)"}

    def test_one_warning_names_every_field(self) -> None:
        class I371Several(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="i371_several",
                signup_fields=["name"],
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str
            org: str
            role: str = "user"
            level: int

        _, caught = _warnings_of(I371Several)

        assert len(caught) == 1
        message = str(caught[0].message)
        assert "'org', 'level'" in message
        assert "'name'" not in message
        assert "'role'" not in message

    def test_read_only_required_field_is_reported(self) -> None:
        """A read-only field cannot join signup_fields, but it still needs a value."""
        from pydantic import Field

        class I371ReadOnly(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i371_read_only")
            id: str | None = None
            email: str
            password: Encrypted
            tenant: str = Field(frozen=True)

        _, caught = _warnings_of(I371ReadOnly)

        assert len(caught) == 1
        assert "'tenant'" in str(caught[0].message)

    def test_each_builder_warns_once(self) -> None:
        class I371Callers(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i371_callers")
            id: str | None = None
            email: str
            password: Encrypted
            name: str

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            AccessGenerator.from_model(I371Callers)
        assert [str(w.message).count("'name'") for w in caught] == [1]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            ModelIntrospector([I371Callers]).introspect()
        assert [str(w.message).count("'name'") for w in caught] == [1]


class TestNoWarning:
    """Models whose required fields are all covered emit nothing."""

    def test_required_fields_in_the_set(self) -> None:
        class I371Listed(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="i371_listed",
                signup_fields=["name", "org"],
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str
            org: str

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert set(build_signup_fields(I371Listed)) == {"email", "password", "name", "org"}

    def test_optional_computed_server_and_virtual_fields(self) -> None:
        class I371Covered(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="i371_covered",
                identifier_field="username",
                password_field="secret",
                server_fields=["status"],
            )
            id: str
            username: str
            secret: Encrypted
            nickname: str | None = None
            role: str = "user"
            tags: list[str] = []
            status: str
            score: Computed[int] = Computed("1")
            created_at: datetime
            friends: ManyToMany("I371Covered")  # type: ignore[valid-type]
            follows: Relation("i371_follows", "I371Covered")  # type: ignore[valid-type]

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            fields = build_signup_fields(I371Covered)
            AccessGenerator.from_model(I371Covered)

        assert fields == {
            "username": "$username",
            "secret": "crypto::argon2::generate($secret)",
            "created_at": "time::now()",
        }
