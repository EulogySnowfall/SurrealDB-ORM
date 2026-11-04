"""
Unit tests for #540 on the v2 line: follow-ups from the #530 review.

Generated migration and ``inspectdb`` model files import
``surreal_orm.surreal_function._surreal_func_from_server`` by that path, so the
helper is part of a contract with files already written to disk even though its
name is private. These tests pin the installed import path (not ``src.*``),
the ``repr()`` round trip those files rely on, and the documented contract.

Run with: pytest tests/test_issue_540_v2_unit.py
"""

import surreal_orm.surreal_function as surreal_function_module
import surreal_orm.utils as utils_module
from surreal_orm import SurrealFunc
from surreal_orm.surreal_function import _surreal_func_from_server

#: A block default the plain constructor refuses (see #412).
BLOCK_DEFAULT = "{ DELETE i540_log WHERE n = 0; RETURN 1; }"


class TestServerHelperImportContract:
    """The path generated files import must keep working."""

    def test_installed_path_resolves_to_the_module_helper(self) -> None:
        assert surreal_function_module._surreal_func_from_server is _surreal_func_from_server

    def test_repr_names_the_helper(self) -> None:
        func = _surreal_func_from_server(BLOCK_DEFAULT)
        assert repr(func) == f"_surreal_func_from_server({BLOCK_DEFAULT!r})"

    def test_repr_evaluates_back_to_an_equal_surreal_func(self) -> None:
        func = _surreal_func_from_server(BLOCK_DEFAULT)
        # Evaluating our own repr is the point: a generated file does exactly
        # this at import. The namespace holds only the name its import line
        # binds, and no builtins.
        namespace = {"__builtins__": {}, "_surreal_func_from_server": _surreal_func_from_server}
        rebuilt = eval(repr(func), namespace)
        assert isinstance(rebuilt, SurrealFunc)
        assert rebuilt == func
        assert hash(rebuilt) == hash(func)
        assert repr(rebuilt) == repr(func)

    def test_docstring_states_the_stability_contract(self) -> None:
        doc = _surreal_func_from_server.__doc__ or ""
        assert "surreal_orm.surreal_function._surreal_func_from_server" in doc
        assert "inspectdb" in doc
        assert "stable" in doc


class TestEscapeSingleQuotesDeprecation:
    def test_deprecation_names_the_release(self) -> None:
        doc = utils_module.escape_single_quotes.__doc__ or ""
        assert ".. deprecated:: 0.21.46" in doc
