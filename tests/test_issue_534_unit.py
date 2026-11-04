"""
#534 — a ``RETURN $auth`` that fails is reported with the server's error.

On an authenticated session, ``signup()`` / ``signin()`` read ``$auth`` on the
ephemeral connection.  When that statement came back as a per-statement
``ERR``, the error text was dropped and the caller was told the session record
was empty (``($auth is empty)``); ``change_password()`` went further and
reported ``Invalid current password`` for a correct password.

An ``ERR`` now raises ``SurrealDbError`` carrying the server's text, and only a
genuinely empty ``$auth`` (``OK`` with ``NONE``) keeps the old message.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.auth.mixins import AuthenticatedUserMixin
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.model_base import (
    BaseSurrealModel,
    SurrealConfigDict,
    SurrealDbError,
    clear_model_registry,
)
from src.surreal_orm.types import TableType
from src.surreal_sdk.types import QueryResult, ResponseStatus

_SERVER_ERROR = "I534 server-side failure: the session could not be evaluated"


@pytest.fixture(autouse=True)
def clear_registry() -> None:
    """Clear model registry before each test."""
    clear_model_registry()


def _model() -> type:
    class I534User(AuthenticatedUserMixin, BaseSurrealModel):
        model_config = SurrealConfigDict(
            table_type=TableType.USER,
            table_name="I534User",
            identifier_field="email",
        )
        id: str | None = None
        email: str
        password: Encrypted

    return I534User


def _ephemeral(first_result: QueryResult, *, signin_ok: bool = True) -> AsyncMock:
    """Ephemeral connection whose ``RETURN $auth`` yields ``first_result``."""
    client = AsyncMock()
    response = MagicMock(success=signin_ok, token="jwt_534" if signin_ok else None, raw={"error": "bad creds"})
    client.signin = AsyncMock(return_value=response)
    client.signup = AsyncMock(return_value=response)
    client.query = AsyncMock(return_value=MagicMock(first_result=first_result))
    client.close = AsyncMock()
    return client


def _err() -> QueryResult:
    return QueryResult(status=ResponseStatus.ERR, result=_SERVER_ERROR)


def _empty() -> QueryResult:
    return QueryResult(status=ResponseStatus.OK, result=None)


def _manager(mock_root: AsyncMock) -> MagicMock:
    return MagicMock(get_client=AsyncMock(return_value=mock_root))


class TestSessionRecordErrorIsReported:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["signin", "signup"])
    async def test_err_raises_with_server_text(self, method: str) -> None:
        model = _model()
        mock_ephemeral = _ephemeral(_err())
        mock_root = AsyncMock()

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=_manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError) as exc_info:
                await getattr(model, method)(email="a@example.com", password="pw")

        message = str(exc_info.value)
        assert _SERVER_ERROR in message
        assert "($auth is empty)" not in message
        mock_ephemeral.close.assert_awaited_once()
        mock_root.query.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["signin", "signup"])
    async def test_empty_auth_keeps_existing_message(self, method: str) -> None:
        model = _model()
        mock_ephemeral = _ephemeral(_empty())
        mock_root = AsyncMock()

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=_manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError, match=r"could not be read \(\$auth is empty\)"):
                await getattr(model, method)(email="a@example.com", password="pw")

        mock_ephemeral.close.assert_awaited_once()
        mock_root.query.assert_not_awaited()


class TestChangePasswordSessionRecordError:
    @pytest.mark.asyncio
    async def test_err_after_correct_password_is_not_invalid_password(self) -> None:
        model = _model()
        mock_ephemeral = _ephemeral(_err())
        mock_root = AsyncMock()

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=_manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError) as exc_info:
                await model.change_password("a@example.com", "correct-old", "new")

        message = str(exc_info.value)
        assert "Invalid current password" not in message
        assert _SERVER_ERROR in message
        # No UPDATE is attempted without a session record.
        mock_root.query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrong_password_is_still_invalid_password(self) -> None:
        model = _model()
        mock_ephemeral = _ephemeral(_err(), signin_ok=False)
        mock_root = AsyncMock()

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=_manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError, match="^Invalid current password$"):
                await model.change_password("a@example.com", "wrong-old", "new")

        mock_ephemeral.query.assert_not_awaited()
        mock_root.query.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_wrong_password_rejected_by_server_is_invalid_password(self) -> None:
        # The SDK raises on a rejected signin; the mixin imports it as ``surreal_sdk``.
        from surreal_sdk.exceptions import AuthenticationError

        model = _model()
        mock_ephemeral = _ephemeral(_empty())
        mock_ephemeral.signin = AsyncMock(side_effect=AuthenticationError("There was a problem with authentication"))
        mock_root = AsyncMock()

        with (
            patch.object(model, "_create_auth_client", new=AsyncMock(return_value=mock_ephemeral)),
            patch("src.surreal_orm.connection_manager.SurrealDBConnectionManager", new=_manager(mock_root)),
        ):
            with pytest.raises(SurrealDbError, match="^Invalid current password$"):
                await model.change_password("a@example.com", "wrong-old", "new")

        mock_root.query.assert_not_awaited()
