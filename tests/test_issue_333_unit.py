"""
Unit tests for issue #333: the SIGNUP clause assigns an explicit sign-up set.

The clause used to assign every model field from a ``$field`` parameter.  It
now assigns only the identifier, the password, the ``signup_fields`` entries
and ``created_at``; every other field keeps its schema or model default.
"""

import re
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import Field

from src.surreal_orm.auth import access as access_module
from src.surreal_orm.auth.access import AccessGenerator
from src.surreal_orm.auth.mixins import AuthenticatedUserMixin
from src.surreal_orm.fields import Computed, Encrypted
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.migrations.operations import DefineAccess, RemoveAccess
from src.surreal_orm.migrations.state import AccessState, SchemaState
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
    clear_model_registry,
)
from src.surreal_orm.types import TableType


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear the model registry before each test."""
    clear_model_registry()


def _signup_params(sql: str) -> set[str]:
    """Return the ``$params`` referenced by the SIGNUP clause of a DEFINE ACCESS."""
    match = re.search(r"SIGNUP \((.*?)\)\n", sql, re.DOTALL)
    assert match is not None, sql
    return set(re.findall(r"\$(\w+)", match.group(1)))


class TestGeneratedSignupClause:
    """The generated DEFINE ACCESS carries only the sign-up set as parameters."""

    def test_default_set_is_identifier_and_password_only(self) -> None:
        class U333Default(BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="u333_default")
            id: str | None = None
            email: str
            password: Encrypted
            name: str = ""
            role: str = "user"
            is_active: bool = True

        access = AccessGenerator.from_model(U333Default)
        assert access is not None
        sql = access.to_surreal_ql()

        assert _signup_params(sql) == {"email", "password"}
        assert access.signup_fields == {
            "email": "$email",
            "password": "crypto::argon2::generate($password)",
        }
        assert "role" not in sql.split("SIGNIN")[0]

    def test_listed_fields_and_created_at(self) -> None:
        from datetime import datetime

        class U333Listed(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="u333_listed",
                signup_fields=["name"],
                encryption_algorithm="bcrypt",  # type: ignore[typeddict-unknown-key]
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str
            role: str = "user"
            created_at: datetime | None = None

        access = AccessGenerator.from_model(U333Listed)
        assert access is not None
        sql = access.to_surreal_ql()

        assert _signup_params(sql) == {"email", "password", "name"}
        assert access.signup_fields == {
            "email": "$email",
            "password": "crypto::bcrypt::generate($password)",
            "name": "$name",
            "created_at": "time::now()",
        }

    def test_custom_identifier_and_password(self) -> None:
        class U333Custom(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="u333_custom",
                identifier_field="username",
                password_field="secret",
            )
            id: str | None = None
            username: str
            secret: Encrypted
            email: str = ""

        assert access_module.build_signup_fields(U333Custom) == {
            "username": "$username",
            "secret": "crypto::argon2::generate($secret)",
        }


class TestSignupFieldsValidation:
    """Invalid ``signup_fields`` entries raise ValueError naming the field and reason."""

    @staticmethod
    def _model(signup_fields: object) -> type[BaseSurrealModel]:
        class U333Invalid(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="u333_invalid",
                signup_fields=signup_fields,  # type: ignore[typeddict-item]
                server_fields=["updated_at"],
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str = ""
            updated_at: str | None = None
            created_at: str | None = None
            display: Computed[str] = Computed("string::uppercase(name)")
            frozen_code: str = Field(default="x", frozen=True)
            legacy_code: str = Field(default="x", json_schema_extra={"readonly": True})

        return U333Invalid

    @pytest.mark.parametrize(
        ("entry", "reason"),
        [
            ("display", "computed field"),
            ("updated_at", "server_fields"),
            ("frozen_code", "read-only"),
            ("legacy_code", "read-only"),
            ("id", "record ID"),
            ("email", "identifier"),
            ("password", "password"),
            ("created_at", "time::now()"),
            ("nickname", "not a field"),
        ],
    )
    def test_rejected_entry(self, entry: str, reason: str) -> None:
        model = self._model([entry])
        with pytest.raises(ValueError, match=re.escape(repr(entry))) as exc:
            AccessGenerator.from_model(model)
        assert reason in str(exc.value)

    def test_rejected_by_introspector_too(self) -> None:
        model = self._model(["display"])
        with pytest.raises(ValueError, match="computed field"):
            ModelIntrospector([model]).introspect()

    def test_not_a_list(self) -> None:
        model = self._model("name")
        with pytest.raises(ValueError, match="must be a list of field names"):
            AccessGenerator.from_model(model)

    def test_valid_entry_accepted(self) -> None:
        model = self._model(["name", "name"])
        access = AccessGenerator.from_model(model)
        assert access is not None
        assert access.signup_fields["name"] == "$name"
        assert access_module.get_signup_parameters(model) == {"email", "password", "name"}


class TestSignupKeywordCheck:
    """signup() rejects keywords outside the sign-up set before any request."""

    @pytest.mark.asyncio
    async def test_extra_keyword_rejected_before_request(self) -> None:
        class U333Kw(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, signup_fields=["name"])
            id: str | None = None
            email: str
            password: Encrypted
            name: str
            role: str = "user"

        create_client = AsyncMock()
        with patch.object(U333Kw, "_create_auth_client", new=create_client):
            with pytest.raises(SurrealDbError, match="signup_fields") as exc:
                await U333Kw.signup(email="a@b.com", password="secret", name="A", role="other")
        assert "role" in str(exc.value)
        create_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_unlisted_keyword_rejected_without_signup_fields(self) -> None:
        class U333KwDefault(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER)
            id: str | None = None
            email: str
            password: Encrypted
            name: str = ""

        create_client = AsyncMock()
        with patch.object(U333KwDefault, "_create_auth_client", new=create_client):
            with pytest.raises(SurrealDbError, match="name"):
                await U333KwDefault.signup(email="a@b.com", password="secret", name="A")
        create_client.assert_not_called()


class TestBuildersAgree:
    """define_access() and the migration introspector emit the same sign-up set."""

    @staticmethod
    def _model() -> type[BaseSurrealModel]:
        from datetime import datetime

        class U333Same(BaseSurrealModel):
            model_config = SurrealConfigDict(
                table_type=TableType.USER,
                table_name="u333_same",
                access_name="u333_account",
                signup_fields=["name"],
                encryption_algorithm="scrypt",  # type: ignore[typeddict-unknown-key]
                token_duration="1h",
                session_duration="24h",
            )
            id: str | None = None
            email: str
            password: Encrypted
            name: str
            role: str = "user"
            created_at: datetime | None = None
            score: Computed[int] = Computed("1")

        return U333Same

    def test_same_definition(self) -> None:
        model = self._model()
        access = AccessGenerator.from_model(model)
        assert access is not None
        state = ModelIntrospector([model]).introspect().tables["u333_same"].access
        assert state is not None

        assert state.name == access.name == "u333_account"
        assert state.signup_fields == access.signup_fields
        assert state.signin_where == access.signin_where
        assert "crypto::scrypt::compare" in state.signin_where
        assert state.signup_fields["password"] == "crypto::scrypt::generate($password)"
        assert set(state.signup_fields) == {"email", "password", "name", "created_at"}
        assert state.duration_token == "1h"
        assert state.duration_session == "24h"

    def test_schema_diff_updates_an_existing_access(self) -> None:
        model = self._model()
        target = ModelIntrospector([model]).introspect()

        # The definition an existing database carries: every field a parameter.
        current = SchemaState()
        current_table = target.tables["u333_same"].clone() if hasattr(target.tables["u333_same"], "clone") else None
        if current_table is None:
            import copy

            current_table = copy.deepcopy(target.tables["u333_same"])
        old = target.tables["u333_same"].access
        assert old is not None
        current_table.access = AccessState(
            name=old.name,
            table=old.table,
            signup_fields={
                "email": "$email",
                "password": "crypto::scrypt::generate($password)",
                "name": "$name",
                "role": "$role",
                "created_at": "time::now()",
            },
            signin_where=old.signin_where,
            duration_token=old.duration_token,
            duration_session=old.duration_session,
        )
        current.tables["u333_same"] = current_table

        operations = current.diff(target)

        removes = [op for op in operations if isinstance(op, RemoveAccess)]
        defines = [op for op in operations if isinstance(op, DefineAccess)]
        assert [op.name for op in removes] == ["u333_account"]
        assert len(defines) == 1
        assert defines[0].signup_fields == old.signup_fields
        assert "role" not in defines[0].signup_fields
