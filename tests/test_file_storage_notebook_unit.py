"""
AC-14: ``examples/18_file_storage.ipynb`` is committed without outputs.

The example notebooks are API tours: the expected output of each cell sits in a
trailing comment and the notebook is executed on a copy, never saved with its
outputs. This checks the committed file: valid nbformat 4 JSON, every code cell
unexecuted and valid Python under IPython's top-level ``await``, the
experimental label first and the ``FULL`` default as the first warning, each
public entry point shown, buckets on the ``memory`` backend only, and the
notebook's buckets removed by its last cell.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest

NOTEBOOK = Path(__file__).resolve().parents[1] / "examples" / "18_file_storage.ipynb"


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


def test_first_cell_says_experimental_then_the_flag_then_full(notebook: dict[str, Any]) -> None:
    first = _source(notebook["cells"][0])
    assert notebook["cells"][0]["cell_type"] == "markdown"
    assert "experimental: follows SurrealDB's experimental `files` capability" in first
    assert "--allow-experimental=files" in first
    warnings = first[first.index("**Requirements and warnings**") :]
    flag, full = warnings.index("--allow-experimental=files"), warnings.index("is `FULL`")
    # The flag is a requirement; the FULL default is the first warning after it.
    assert flag < full
    assert "transactional" in warnings[full:]


@pytest.mark.parametrize(
    "call",
    [
        "DefineBucket(",
        "AlterBucket(",
        "RemoveBucket(",
        "parse_define_bucket(",
        "FileRef.parse(",
        "client.file_put(",
        "client.file_get(",
        "client.file_list(",
        ".upload_file(",
        ".download_file(",
        ".file_info(",
        ".delete_file(",
        "overwrite=False",
        "except FileExistsError",
        "DefineEvent(",
        "$action INSIDE",
    ],
)
def test_notebook_shows_each_entry_point(notebook: dict[str, Any], call: str) -> None:
    assert any(call in _source(cell) for cell in _code_cells(notebook))


def test_buckets_use_the_memory_backend_only(notebook: dict[str, Any]) -> None:
    for cell in _code_cells(notebook):
        for backend in re.findall(r"backend=\"([^\"]*)\"", _source(cell)):
            assert backend == "memory", cell.get("id")


def test_last_cell_removes_the_notebook_buckets(notebook: dict[str, Any]) -> None:
    last = _source(_code_cells(notebook)[-1])
    assert "for name in BUCKETS:" in last
    assert "RemoveBucket(name).forwards()" in last
