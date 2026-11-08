"""
#421 — follow-ups from the 0.35.6 reviews.

1. A ``DEFAULT`` the server reports and ``SurrealFunc()`` refuses (a block
   ``{ DELETE ...; RETURN 1; }``) was built through ``SurrealFunc.__new__``.
   Its ``repr()`` was still ``SurrealFunc('...')``, which is what
   ``MigrationGenerator`` and ``inspectdb`` write: importing the generated file
   ran the constructor again and raised ``ValueError``.
2. The legacy ``str`` expression default warning did not name the field.
5. ``validate_token()`` / ``authenticate_token()`` opened an ephemeral
   connection and made two requests before rejecting a token whose decoded
   ``AC`` / ``NS`` / ``DB`` claims name another access, namespace or database.

Run with: pytest tests/test_issue_421_unit.py
"""

from __future__ import annotations

import base64
import importlib.util
import json
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.surreal_orm.auth import AuthenticatedUserMixin
from src.surreal_orm.connection_manager import SurrealDBConnectionManager
from src.surreal_orm.fields import Encrypted
from src.surreal_orm.migrations.define_parser import parse_define_field
from src.surreal_orm.migrations.generator import MigrationGenerator
from src.surreal_orm.migrations.introspector import ModelIntrospector
from src.surreal_orm.migrations.model_generator import ModelCodeGenerator
from src.surreal_orm.migrations.operations import AddField, AlterField, _render_define_field
from src.surreal_orm.migrations.state import FieldState, SchemaState, TableState
from src.surreal_orm.model_base import BaseSurrealModel, SurrealConfigDict
from src.surreal_orm.surreal_function import SurrealFunc, _is_from_server, _surreal_func_from_server
from src.surreal_orm.types import TableType
from src.surreal_sdk.types import AuthResponse

# ------------------------------------------------------------------ point 1

#: ``INFO FOR TABLE`` on SurrealDB 3.3.5 for
#: ``DEFINE FIELD n ON i421 TYPE int DEFAULT { DELETE i421_log WHERE n = 0; RETURN 1; }``.
BLOCK_DEFINITION = "DEFINE FIELD n ON i421 TYPE int DEFAULT { DELETE i421_log WHERE n = 0; RETURN 1; } PERMISSIONS FULL"
BLOCK_EXPRESSION = "{ DELETE i421_log WHERE n = 0; RETURN 1; }"
HELPER_IMPORT = "from surreal_orm.surreal_function import _surreal_func_from_server"


def _load(path: Path) -> ModuleType:
    """Import a generated migration file the way the executor does."""
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parsed_block_default() -> SurrealFunc:
    default = parse_define_field(BLOCK_DEFINITION).default
    assert isinstance(default, SurrealFunc)
    return default


class TestServerDefaultInGeneratedMigration:
    def test_parsed_block_default_is_one_the_constructor_refuses(self) -> None:
        default = _parsed_block_default()
        assert default.expression == BLOCK_EXPRESSION
        assert _is_from_server(default)
        with pytest.raises(ValueError, match="dangerous"):
            SurrealFunc(BLOCK_EXPRESSION)

    def test_generated_migration_with_block_default_imports(self, tmp_path: Path) -> None:
        default = _parsed_block_default()
        new = FieldState(name="n", field_type="int", default=default)
        old = FieldState(name="n", field_type="int", default=default, comment="before")
        operations = [
            AddField.from_field_state("i421", new),
            AlterField.from_field_states("i421", new, old),
        ]
        path = MigrationGenerator(tmp_path).generate(name="block_default", operations=operations)

        # The generated file imports ``surreal_orm``, the tests ``src.surreal_orm``
        # (two class objects), so the expressions are compared.
        loaded = _load(path).migration.operations
        assert type(loaded[0].default).__name__ == "SurrealFunc"
        assert loaded[0].default.expression == default.expression
        assert loaded[1].default.expression == default.expression
        assert loaded[1].previous_default.expression == default.expression

    def test_loaded_block_default_renders_the_server_text(self, tmp_path: Path) -> None:
        op = AddField.from_field_state("i421", FieldState(name="n", field_type="int", default=_parsed_block_default()))
        path = MigrationGenerator(tmp_path).generate(name="block_render", operations=[op])
        sql = _load(path).migration.operations[0].forwards()
        assert sql.endswith(f" DEFAULT {BLOCK_EXPRESSION};")

    def test_helper_imported_only_when_needed(self, tmp_path: Path) -> None:
        server = AddField(table="t", name="a", field_type="int", default=_surreal_func_from_server(BLOCK_EXPRESSION))
        source = MigrationGenerator(tmp_path)._render_migration("0001_x", [server], [])
        assert HELPER_IMPORT in source
        assert "from surreal_orm import SurrealFunc" not in source

    def test_both_imports_when_both_forms_are_present(self, tmp_path: Path) -> None:
        operations = [
            AddField(table="t", name="a", field_type="int", default=_surreal_func_from_server(BLOCK_EXPRESSION)),
            AddField(table="t", name="b", field_type="datetime", default=SurrealFunc("time::now()")),
        ]
        path = MigrationGenerator(tmp_path).generate(name="both", operations=operations)
        source = path.read_text()
        assert HELPER_IMPORT in source
        assert "from surreal_orm import SurrealFunc" in source
        loaded = _load(path).migration.operations
        assert [op.default.expression for op in loaded] == [BLOCK_EXPRESSION, "time::now()"]


class TestServerDefaultInInspectdbModels:
    """``inspectdb`` writes ``repr(default)`` into a models module too."""

    @staticmethod
    def _models_source(default: SurrealFunc) -> str:
        table = TableState(name="i421_model", fields={"n": FieldState(name="n", field_type="int", default=default)})
        return ModelCodeGenerator().generate(SchemaState(tables={"i421_model": table}))

    def test_generated_models_with_block_default_execute(self) -> None:
        default = _parsed_block_default()
        source = self._models_source(default)
        assert HELPER_IMPORT in source
        assert "from surreal_orm import SurrealFunc\n" not in source
        namespace: dict[str, object] = {}
        exec(compile(source, "<i421_models>", "exec"), namespace)
        model = namespace["I421Model"]
        loaded = model.model_fields["n"].default  # type: ignore[attr-defined]
        assert type(loaded).__name__ == "SurrealFunc"
        assert loaded.expression == default.expression

    def test_generated_models_with_plain_default_unchanged(self) -> None:
        source = self._models_source(SurrealFunc("time::now()"))
        assert "from surreal_orm import SurrealFunc" in source
        assert "default=SurrealFunc('time::now()')" in source
        assert "_surreal_func_from_server" not in source


class TestHandWrittenSurrealFuncUnchanged:
    def test_accepted_expression_still_parses_as_plain_surrealfunc(self) -> None:
        parsed = parse_define_field("DEFINE FIELD c ON t TYPE datetime DEFAULT time::now() PERMISSIONS FULL")
        assert repr(parsed.default) == "SurrealFunc('time::now()')"
        assert isinstance(parsed.default, SurrealFunc)
        assert not _is_from_server(parsed.default)

    def test_repr_equality_and_hash(self) -> None:
        func = SurrealFunc("time::now()")
        assert repr(func) == "SurrealFunc('time::now()')"
        assert func == SurrealFunc("time::now()")
        assert hash(func) == hash("time::now()")
        assert not _is_from_server(func)

    def test_plain_surrealfunc_written_as_constructor_call(self, tmp_path: Path) -> None:
        op = AlterField.from_field_states(
            "t",
            FieldState(name="c", field_type="datetime", default=SurrealFunc("time::now()")),
            FieldState(name="c", field_type="datetime"),
        )
        source = MigrationGenerator(tmp_path)._render_migration("0002_x", [op], [])
        assert "default=SurrealFunc('time::now()')" in source
        assert "from surreal_orm import SurrealFunc" in source
        assert "_surreal_func_from_server" not in source

    def test_handwritten_refused_expression_still_raises(self) -> None:
        with pytest.raises(ValueError, match="dangerous"):
            SurrealFunc("{ DELETE x; RETURN 1; }")


class TestBothFormsCompareEqual:
    """The schema diff compares a database default with a model default."""

    def test_equal_and_same_hash(self) -> None:
        server = _surreal_func_from_server("time::now()")
        plain = SurrealFunc("time::now()")
        assert server == plain
        assert plain == server
        assert hash(server) == hash(plain)
        assert {server, plain} == {plain}

    def test_server_form_repr_is_the_helper_call(self) -> None:
        server = _surreal_func_from_server(BLOCK_EXPRESSION)
        assert repr(server) == f"_surreal_func_from_server({BLOCK_EXPRESSION!r})"
        assert isinstance(server, SurrealFunc)
        # The repr is evaluated the way a generated file runs it (constant input).
        assert eval(repr(server), {"_surreal_func_from_server": _surreal_func_from_server}) == server

    def test_server_form_renders_in_ddl(self) -> None:
        sql = _render_define_field(
            "t", "n", overwrite=True, field_type="int", default=_surreal_func_from_server(BLOCK_EXPRESSION)
        )
        assert sql == f"DEFINE FIELD OVERWRITE n ON t TYPE int DEFAULT {BLOCK_EXPRESSION};"


# ------------------------------------------------------------------ point 2


class I421Legacy(BaseSurrealModel):
    model_config = SurrealConfigDict(table_name="i421_unit_legacy")

    id: str | None = None
    stamp: str = "time::now()"


class TestLegacyDefaultWarningNamesTheField:
    def test_introspector_warning_starts_with_model_and_field(self) -> None:
        with pytest.warns(DeprecationWarning, match=r"^I421Legacy\.stamp: .*SurrealFunc") as record:
            ModelIntrospector([I421Legacy]).introspect()
        assert len([w for w in record if issubclass(w.category, DeprecationWarning)]) == 1

    def test_add_field_warning_names_table_and_field(self) -> None:
        with pytest.warns(DeprecationWarning, match=r"^i421_t\.stamp: .*SurrealFunc"):
            AddField(table="i421_t", name="stamp", field_type="any", default="time::now()").forwards()

    def test_alter_field_warning_names_table_and_field_both_ways(self) -> None:
        op = AlterField(
            table="i421_t",
            name="stamp",
            field_type="any",
            default="$value",
            previous_type="any",
            previous_default="rand::uuid()",
        )
        with pytest.warns(DeprecationWarning, match=r"^i421_t\.stamp: .*SurrealFunc"):
            op.forwards()
        with pytest.warns(DeprecationWarning, match=r"^i421_t\.stamp: .*SurrealFunc"):
            op.backwards()


# ------------------------------------------------------------------ point 5

NS = "i421_ns"
DB = "i421_db"
RECORD = "i421_user:a"


def _jwt(claims: dict[str, object]) -> str:
    """Build an unsigned JWT-shaped token carrying ``claims``."""

    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'HS512'})}.{part(claims)}.c2ln"


def _token(access: str, *, ns: object = NS, db: object = DB) -> str:
    """A token as SurrealDB issues it for a record access."""
    return _jwt({"NS": ns, "DB": db, "AC": access, "ID": RECORD, "exp": int(time.time()) + 600})


def _client() -> AsyncMock:
    """Ephemeral connection mock on ``NS``/``DB`` whose ``RETURN $auth`` yields ``RECORD``."""
    client = AsyncMock()
    client.namespace = NS
    client.database = DB
    client.authenticate = AsyncMock(return_value=AuthResponse(token="tok", success=True, raw={}))
    client.query = AsyncMock(return_value=MagicMock(first_result=MagicMock(result=RECORD)))
    client.close = AsyncMock()
    return client


class I421User(AuthenticatedUserMixin, BaseSurrealModel):
    model_config = SurrealConfigDict(
        table_type=TableType.USER, table_name="i421_user", access_name="i421_access", connection="i421_conn"
    )
    id: str | None = None
    email: str
    password: Encrypted


#: Tokens whose claims name another access, namespace or database.
FOREIGN_TOKENS = [
    _token("i421_other"),
    _token("i421_access", ns="other_ns"),
    _token("i421_access", db="other_db"),
    _jwt({"NS": NS, "DB": DB, "ID": RECORD}),
    _token("I421_ACCESS"),
]
FOREIGN_IDS = ["other-access", "other-ns", "other-db", "no-ac", "other-case-ac"]


@pytest.fixture
def _registry() -> Iterator[None]:
    """Register the model's connection on ``NS``/``DB``; reset the token cache."""
    mgr = SurrealDBConnectionManager
    saved_configs = dict(mgr._configs)
    mgr.add_connection("i421_conn", url="http://localhost:8000", user="root", password="root", namespace=NS, database=DB)
    AuthenticatedUserMixin._token_cache.clear()
    AuthenticatedUserMixin._token_cache_ttl = 300
    AuthenticatedUserMixin._token_cache_max_size = 1000
    yield
    AuthenticatedUserMixin._token_cache.clear()
    mgr._configs.clear()
    mgr._configs.update(saved_configs)


@pytest.mark.usefixtures("_registry")
class TestForeignTokenRejectedLocally:
    @pytest.mark.parametrize("token", FOREIGN_TOKENS, ids=FOREIGN_IDS)
    @pytest.mark.parametrize("use_cache", [True, False], ids=["cache", "no-cache"])
    async def test_validate_token_makes_no_request(self, token: str, use_cache: bool) -> None:
        create = AsyncMock(return_value=_client())
        with patch.object(I421User, "_create_auth_client", new=create):
            assert await I421User.validate_token(token, use_cache=use_cache) is None
        create.assert_not_awaited()
        assert AuthenticatedUserMixin._token_cache == {}

    @pytest.mark.parametrize("token", FOREIGN_TOKENS, ids=FOREIGN_IDS)
    async def test_authenticate_token_makes_no_request(self, token: str) -> None:
        create = AsyncMock(return_value=_client())
        fetch = AsyncMock(return_value=MagicMock())
        with (
            patch.object(I421User, "_create_auth_client", new=create),
            patch.object(I421User, "_fetch_record_by_id", new=fetch),
        ):
            assert await I421User.authenticate_token(token) is None
        create.assert_not_awaited()
        fetch.assert_not_awaited()


@pytest.mark.usefixtures("_registry")
class TestTokenPassingThePreCheckStillGoesToTheServer:
    async def test_own_token_is_validated_by_the_server(self) -> None:
        client = _client()
        with patch.object(I421User, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await I421User.validate_token(_token("i421_access"), use_cache=False) == RECORD
        client.authenticate.assert_awaited_once()
        client.close.assert_awaited_once()

    async def test_own_token_rejected_by_the_server_returns_none(self) -> None:
        client = _client()
        client.authenticate = AsyncMock(return_value=AuthResponse(token="", success=False, raw={}))
        with patch.object(I421User, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await I421User.validate_token(_token("i421_access"), use_cache=False) is None
        client.authenticate.assert_awaited_once()

    async def test_post_check_still_runs_on_the_client_it_built(self) -> None:
        # Claims match the configuration, but the client reports another
        # database: the post-check, unchanged, still rejects the token.
        client = _client()
        client.database = "moved_db"
        with patch.object(I421User, "_create_auth_client", new=AsyncMock(return_value=client)):
            assert await I421User.validate_token(_token("i421_access"), use_cache=False) is None
        client.authenticate.assert_awaited_once()

    async def test_authenticate_token_own_token_returns_the_user(self) -> None:
        user = MagicMock()
        client = _client()
        with (
            patch.object(I421User, "_create_auth_client", new=AsyncMock(return_value=client)),
            patch.object(I421User, "_fetch_record_by_id", new=AsyncMock(return_value=user)),
        ):
            assert await I421User.authenticate_token(_token("i421_access")) == (user, RECORD)
        client.authenticate.assert_awaited_once()

    @pytest.mark.parametrize("token", ["opaque-token", "a.b.c", "a.bm90LWpzb24.c"], ids=["opaque", "bad-b64", "not-json"])
    async def test_undecodable_token_still_goes_to_the_server(self, token: str) -> None:
        client = _client()
        create = AsyncMock(return_value=client)
        with patch.object(I421User, "_create_auth_client", new=create):
            assert await I421User.validate_token(token, use_cache=False) is None
        create.assert_awaited_once()
        client.authenticate.assert_awaited_once_with(token)


class TestPreCheckWithoutConfiguration:
    async def test_no_config_leaves_the_error_to_the_auth_client(self) -> None:
        """Without a connection config the pre-check judges nothing; the client raises as before."""
        from src.surreal_orm.model_base import SurrealDbError

        mgr = SurrealDBConnectionManager
        saved_configs = dict(mgr._configs)
        mgr._configs.pop("i421_conn", None)
        try:
            with pytest.raises(SurrealDbError, match="i421_conn"):
                await I421User.validate_token(_token("i421_other"), use_cache=False)
        finally:
            mgr._configs.clear()
            mgr._configs.update(saved_configs)
