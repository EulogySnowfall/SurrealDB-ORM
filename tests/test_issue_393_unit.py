"""
#393 — validate_token() / authenticate_token() check which access issued the token.

Two models can share a table with different ``access_name`` values.  The server
accepts a token issued by any access definition of the database, and ``$auth``
is a record of the shared table in both cases, so the table check alone let
each model accept the other model's tokens.  The token's ``AC`` claim must name
the model's access, and its ``NS`` / ``DB`` claims the namespace and database of
the model's connection.  The cache key carries the access name.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.auth import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.types import TableType
from src.surreal_sdk.types import AuthResponse

NS = "i393_ns"
DB = "i393_db"
RECORD = "i393_user:a"


def _jwt(claims: dict[str, object]) -> str:
    """Build an unsigned JWT-shaped token carrying ``claims``."""

    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS512'})}.{part(claims)}.c2ln"


def _token(access: str, *, ns: object = NS, db: object = DB, **extra: object) -> str:
    """A token as SurrealDB issues it for a record access."""
    claims: dict[str, object] = {"NS": ns, "DB": db, "AC": access, "ID": RECORD, "exp": int(time.time()) + 600}
    claims.update(extra)
    return _jwt(claims)


def _client(auth: object = RECORD, *, success: bool = True) -> AsyncMock:
    """Ephemeral connection mock on ``NS``/``DB`` whose ``RETURN $auth`` yields ``auth``."""
    client = AsyncMock()
    client.namespace = NS
    client.database = DB
    client.authenticate = AsyncMock(return_value=AuthResponse(token="tok", success=success, raw={}))
    client.query = AsyncMock(return_value=MagicMock(first_result=MagicMock(result=auth)))
    client.close = AsyncMock()
    return client


def _models() -> tuple[type, type]:
    """Two models on one table, each with its own access definition."""

    class I393Public(AuthenticatedUserMixin, BaseSurrealModel):
        model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i393_user", access_name="i393_public")
        id: str | None = None
        email: str
        password: Encrypted

    class I393Staff(AuthenticatedUserMixin, BaseSurrealModel):
        model_config = SurrealConfigDict(table_type=TableType.USER, table_name="i393_user", access_name="i393_staff")
        id: str | None = None
        email: str
        password: Encrypted

    return I393Public, I393Staff


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:
    AuthenticatedUserMixin._token_cache.clear()
    AuthenticatedUserMixin._token_cache_ttl = 300
    AuthenticatedUserMixin._token_cache_max_size = 1000
    yield
    AuthenticatedUserMixin._token_cache.clear()


class TestValidateTokenAccessClaim:
    """validate_token() accepts only tokens of the model's own access."""

    @pytest.mark.parametrize("use_cache", [True, False], ids=["cache", "no-cache"])
    async def test_token_of_other_access_returns_none(self, use_cache: bool) -> None:
        public, staff = _models()
        token = _token("i393_public")
        client = _client()
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await staff.validate_token(token, use_cache=use_cache) is None
        assert AuthenticatedUserMixin._token_cache == {}
        client.close.assert_awaited_once()

    @pytest.mark.parametrize("use_cache", [True, False], ids=["cache", "no-cache"])
    async def test_token_of_own_access_is_accepted(self, use_cache: bool) -> None:
        public, staff = _models()
        with patch.object(public, "_create_auth_client", new=AsyncMock(return_value=_client())):
            assert await public.validate_token(_token("i393_public"), use_cache=use_cache) == RECORD
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=_client())):
            assert await staff.validate_token(_token("i393_staff"), use_cache=use_cache) == RECORD

    async def test_cached_entry_of_one_model_is_not_a_hit_for_the_other(self) -> None:
        public, staff = _models()
        token = _token("i393_public")
        with patch.object(public, "_create_auth_client", new=AsyncMock(return_value=_client())):
            assert await public.validate_token(token) == RECORD
        assert len(AuthenticatedUserMixin._token_cache) == 1

        client = _client()
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await staff.validate_token(token) is None
        # Not served from the public model's entry: the server was asked.
        client.authenticate.assert_awaited_once_with(token)

    async def test_cache_keys_differ_by_access_name(self) -> None:
        public, staff = _models()
        assert public._token_cache_key("t") != staff._token_cache_key("t")
        assert public._token_cache_key("t")[1:] == ("i393_user", "i393_public", "t")

    async def test_own_cached_entry_is_still_served(self) -> None:
        _, staff = _models()
        token = _token("i393_staff")
        client = _client()
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await staff.validate_token(token) == RECORD
            assert await staff.validate_token(token) == RECORD
        client.authenticate.assert_awaited_once_with(token)

    @pytest.mark.parametrize(
        "token",
        [
            _token("i393_staff", ns="other_ns"),
            _token("i393_staff", db="other_db"),
        ],
        ids=["other-ns", "other-db"],
    )
    @pytest.mark.parametrize("use_cache", [True, False], ids=["cache", "no-cache"])
    async def test_other_namespace_or_database_returns_none(self, token: str, use_cache: bool) -> None:
        _, staff = _models()
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=_client())):
            assert await staff.validate_token(token, use_cache=use_cache) is None
        assert AuthenticatedUserMixin._token_cache == {}

    @pytest.mark.parametrize(
        "token",
        [
            "opaque-token",
            _jwt({"NS": NS, "DB": DB, "ID": RECORD}),
            _jwt({"DB": DB, "AC": "i393_staff", "ID": RECORD}),
            _jwt({"NS": NS, "AC": "i393_staff", "ID": RECORD}),
            _jwt({"ns": NS, "db": DB, "ac": "i393_staff", "ID": RECORD}),
            _token("i393_staff", ns=None),
            _token("I393_STAFF"),
        ],
        ids=["opaque", "no-ac", "no-ns", "no-db", "lowercase-claims", "null-ns", "other-case-ac"],
    )
    async def test_missing_or_unreadable_claims_return_none(self, token: str) -> None:
        _, staff = _models()
        with patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=_client())):
            assert await staff.validate_token(token) is None

    async def test_default_access_name_still_validates_own_tokens(self) -> None:
        class I393Default(AuthenticatedUserMixin, BaseSurrealModel):
            model_config = SurrealConfigDict(table_type=TableType.USER, table_name="I393Default")
            id: str | None = None
            email: str
            password: Encrypted

        assert I393Default.get_access_name() == "i393default_auth"
        token = _token("i393default_auth")
        client = _client("I393Default:a")
        with patch.object(I393Default, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await I393Default.validate_token(token) == "I393Default:a"
            assert await I393Default.validate_token(token) == "I393Default:a"
        client.authenticate.assert_awaited_once_with(token)

    async def test_invalidate_token_removes_the_entries_of_every_access(self) -> None:
        public, staff = _models()
        token = _token("i393_public")
        other = _token("i393_public", jti="other")
        with patch.object(public, "_create_auth_client", new=AsyncMock(return_value=_client())):
            await public.validate_token(token)
            await public.validate_token(other)
        # An entry for the same token under the staff model's key.
        AuthenticatedUserMixin._token_cache[staff._token_cache_key(token)] = (RECORD, time.monotonic() + 300, time.time() + 300)
        assert len(AuthenticatedUserMixin._token_cache) == 3

        staff.invalidate_token_cache(token)

        assert list(AuthenticatedUserMixin._token_cache) == [public._token_cache_key(other)]


class TestAuthenticateTokenAccessClaim:
    """authenticate_token() applies the same AC / NS / DB checks."""

    @pytest.mark.parametrize(
        "token",
        [
            _token("i393_public"),
            _token("i393_staff", ns="other_ns"),
            _token("i393_staff", db="other_db"),
            "opaque-token",
        ],
        ids=["other-access", "other-ns", "other-db", "opaque"],
    )
    async def test_foreign_token_returns_none(self, token: str) -> None:
        _, staff = _models()
        fetch = AsyncMock(return_value=MagicMock())
        client = _client()
        with (
            patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=client)),
            patch.object(staff, "_fetch_record_by_id", new=fetch),
        ):
            assert await staff.authenticate_token(token) is None
        fetch.assert_not_awaited()
        client.close.assert_awaited_once()

    async def test_own_token_returns_the_user(self) -> None:
        _, staff = _models()
        user = MagicMock()
        fetch = AsyncMock(return_value=user)
        with (
            patch.object(staff, "_create_auth_client", new=AsyncMock(return_value=_client())),
            patch.object(staff, "_fetch_record_by_id", new=fetch),
        ):
            assert await staff.authenticate_token(_token("i393_staff")) == (user, RECORD)
        fetch.assert_awaited_once_with(RECORD)
