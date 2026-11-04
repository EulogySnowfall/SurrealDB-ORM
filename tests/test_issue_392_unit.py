"""Unit tests for #392: follow-ups from the 0.21.25 reviews (v2).

* ``QuerySet.variables()`` refuses the names the ORM binds itself (``_f0``,
  ``_s0``, ``_bu0``, ``_sv_<field>``, ``_knn_vec``, ``_geo_max``,
  ``_hybrid_vec``, ``_hybrid_text``, ``_pfp``). Filter bindings are merged into
  the same dict after the declared ones, so a declared ``_f0`` was silently
  replaced by the ORM's value.
* ``_read_session_record()`` ignored a per-statement ``ERR`` for
  ``RETURN $auth``: the error text was used as the record ID, so ``signin()`` /
  ``signup()`` raised ``DoesNotExist`` and ``change_password()`` ran its
  ``UPDATE`` against it, instead of the documented ``SurrealDbError``. Since
  #534 an ``ERR`` raises ``SurrealDbError`` carrying the server's text, and only
  an empty ``$auth`` returns ``None``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.auth.mixins import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.model_base import SurrealDbError
from src.surreal_orm.types import TableType
from src.surreal_sdk.types import QueryResponse, QueryResult, ResponseStatus


class U392Item(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="u392_item")
    id: str | None = None
    name: str = ""


class U392User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER,
        table_name="u392_user",
        identifier_field="email",
    )
    id: str | None = None
    email: str
    password: Encrypted


# ---------------------------------------------------------------------------
# .variables() and the ORM's reserved binding names
# ---------------------------------------------------------------------------


class TestReservedVariableNames:
    @pytest.mark.parametrize(
        "name",
        [
            "_f0",
            "_f12",
            "_s0",
            "_s3",
            "_bu0",
            "_bu7",
            "_sv_name",
            "_sv_",
            "_knn_vec",
            "_geo_max",
            "_hybrid_vec",
            "_hybrid_text",
            "_pfp",
        ],
    )
    def test_reserved_name_raises(self, name: str) -> None:
        with pytest.raises(ValueError, match="reserved"):
            U392Item.objects().variables(**{name: 1})

    def test_reserved_name_among_valid_ones_raises(self) -> None:
        with pytest.raises(ValueError, match="_f0"):
            U392Item.objects().variables(status="active", _f0=1)

    @pytest.mark.parametrize(
        "name",
        ["_foo", "_fx", "_f", "_s", "_bu", "_sv", "_f0x", "_knn", "_knn_vec2", "_geo", "_hybrid", "f0", "status", "_F0"],
    )
    def test_other_names_are_accepted(self, name: str) -> None:
        qs = U392Item.objects().variables(**{name: 1})
        assert qs._variables == {name: 1}
        assert qs._declared_variables == frozenset({name})

    def test_declared_value_is_not_replaced_by_a_filter_binding(self) -> None:
        # The case the reserved names guard: a declared name that is not
        # reserved keeps its value when filters are compiled.
        qs = U392Item.objects().filter(name="$wanted", id__in=["a"]).variables(wanted="x")
        query = qs._compile_query()
        assert "name = $wanted" in query
        assert qs._variables["wanted"] == "x"


# ---------------------------------------------------------------------------
# _read_session_record() and a per-statement ERR
# ---------------------------------------------------------------------------


def _err_response(text: str = "There was a problem with the database: boom") -> QueryResponse:
    return QueryResponse(results=[QueryResult(status=ResponseStatus.ERR, result=text)])


def _ephemeral(response: QueryResponse) -> AsyncMock:
    client = AsyncMock()
    auth = MagicMock(success=True, token="jwt_392", raw={})
    client.signin = AsyncMock(return_value=auth)
    client.signup = AsyncMock(return_value=auth)
    client.query = AsyncMock(return_value=response)
    client.close = AsyncMock()
    return client


def _manager(root: AsyncMock) -> MagicMock:
    config = MagicMock(namespace="ns392", database="db392")
    return MagicMock(get_client=AsyncMock(return_value=root), get_config=MagicMock(return_value=config))


class TestSessionRecordError:
    @pytest.mark.asyncio
    async def test_read_session_record_raises_on_err(self) -> None:
        client = _ephemeral(_err_response())
        with pytest.raises(SurrealDbError, match="boom"):
            await U392User._read_session_record(client)

    @pytest.mark.asyncio
    async def test_read_session_record_returns_ok_value(self) -> None:
        ok = QueryResponse(results=[QueryResult(status=ResponseStatus.OK, result="u392_user:a")])
        assert await U392User._read_session_record(_ephemeral(ok)) == "u392_user:a"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["signin", "signup"])
    async def test_signin_signup_raise_surreal_db_error(self, method: str) -> None:
        ephemeral = _ephemeral(_err_response())
        root = AsyncMock()
        root.query = AsyncMock(return_value=MagicMock(is_empty=True, all_records=[]))

        with (
            patch.object(U392User, "_create_auth_client", new=AsyncMock(return_value=ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=_manager(root)),
        ):
            with pytest.raises(SurrealDbError, match="session record could not be read") as excinfo:
                await getattr(U392User, method)(email="a@example.com", password="pw")

        assert not isinstance(excinfo.value, U392User.DoesNotExist)
        ephemeral.close.assert_awaited_once()
        # The error text is never used as a record ID.
        root.query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_password_raises_without_update(self) -> None:
        ephemeral = _ephemeral(_err_response())
        root = AsyncMock()
        root.query = AsyncMock(return_value=MagicMock(all_records=[]))

        with (
            patch.object(U392User, "_create_auth_client", new=AsyncMock(return_value=ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=_manager(root)),
        ):
            with pytest.raises(SurrealDbError) as excinfo:
                await U392User.change_password("a@example.com", "old", "new")

        assert not isinstance(excinfo.value, U392User.DoesNotExist)
        # No UPDATE is sent for a session record that could not be read.
        root.query.assert_not_awaited()
