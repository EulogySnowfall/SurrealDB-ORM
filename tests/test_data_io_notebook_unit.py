"""
AC-13: ``examples/17_data_import_export.ipynb`` is committed without outputs.

The example notebooks are API tours: the expected output of each cell sits in a
trailing comment and the notebook is executed on a copy, never saved with its
outputs (an executed save turns any later edit into a diff of every cell). This
checks the committed file: valid nbformat 4 JSON, every code cell unexecuted,
every code cell valid Python under IPython's top-level ``await``, each public
entry point of the data import/export feature shown, and no SurrealML call.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

NOTEBOOK = Path(__file__).resolve().parents[1] / "examples" / "17_data_import_export.ipynb"


@pytest.fixture(scope="module")
def notebook() -> dict[str, Any]:
    assert NOTEBOOK.is_file(), f"missing {NOTEBOOK}"
    data: dict[str, Any] = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    return data


def _code_cells(notebook: dict[str, Any]) -> list[dict[str, Any]]:
    return [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]


def _source(cell: dict[str, Any]) -> str:
    source = cell["source"]
    return source if isinstance(source, str) else "".join(source)


def test_notebook_is_nbformat_4_with_code_cells(notebook: dict[str, Any]) -> None:
    assert notebook["nbformat"] == 4
    assert _code_cells(notebook)


def test_every_code_cell_is_committed_without_outputs(notebook: dict[str, Any]) -> None:
    for cell in _code_cells(notebook):
        assert cell["outputs"] == [], cell.get("id")
        assert cell["execution_count"] is None, cell.get("id")


def test_every_code_cell_parses_with_top_level_await(notebook: dict[str, Any]) -> None:
    flags = ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
    for cell in _code_cells(notebook):
        compile(_source(cell), f"<cell {cell.get('id')}>", "exec", flags=flags)


@pytest.mark.parametrize(
    "call",
    [
        ".export_jsonl(",
        ".iter_jsonl(typed=False)",
        ".import_jsonl(",
        "client.export_jsonl(",
        "client.import_jsonl(",
        "SurrealDBConnectionManager.dump_database(",
        "SurrealDBConnectionManager.restore_database(",
        'SurrealDBConnectionManager.using("copy")',
    ],
)
def test_notebook_shows_each_entry_point(notebook: dict[str, Any], call: str) -> None:
    assert any(call in _source(cell) for cell in _code_cells(notebook))


def test_notebook_has_no_surrealml_call(notebook: dict[str, Any]) -> None:
    for cell in notebook["cells"]:
        source = _source(cell)
        assert "/ml" not in source, cell.get("id")
        assert "ml::" not in source, cell.get("id")
