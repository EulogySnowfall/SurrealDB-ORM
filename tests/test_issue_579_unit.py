"""#579: `main`'s side of the permanent `v3` branch.

`v3` carries the SurrealDB 3.x line once `main` moves to 4.x. On `main` that
means: the branch guard refuses `v3` heads as it refuses `v2` ones, and
`tag-release.yml` on `main` names `main` only (`v3` has its own copy, which is
the one GitHub runs for a push to `v3`).

The guard's condition is a GitHub expression. It is evaluated here by a small
translator for the subset it uses (`==`, `&&`, `||`, `startsWith()`, string
literals, `github.base_ref` / `github.head_ref`), which refuses anything else,
so an edit into another shape fails these tests instead of being misread.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml required for workflow lint tests")

WORKFLOW_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'[^']*')|(?P<op>==|&&|\|\||[(),])|(?P<fn>startsWith)|(?P<ctx>github\.(?:base_ref|head_ref)))"
)


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WORKFLOW_DIR / name).read_text(encoding="utf-8"))
    return data


def _triggers(data: dict[str, Any]) -> dict[str, Any]:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    on: dict[str, Any] = data.get("on", data.get(True))
    return on


def _to_python(expr: str) -> str:
    out: list[str] = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN.match(expr, pos)
        if not m or m.end() == pos:
            raise AssertionError(f"unsupported expression at {expr[pos:]!r}")
        pos = m.end()
        if m.group("str") is not None:
            out.append(repr(m.group("str")[1:-1]))
        elif m.group("fn"):
            out.append("_starts")
        elif m.group("ctx"):
            out.append(m.group("ctx").split(".")[1])
        else:
            out.append({"&&": " and ", "||": " or "}.get(m.group("op"), m.group("op")))
    return "".join(out)


def _guard_blocks(base: str, head: str) -> bool:
    data = _load("ci.yml")
    (step,) = data["jobs"]["branch-guard"]["steps"]
    source = _to_python(" ".join(str(step["if"]).split()))
    namespace = {"_starts": lambda s, p: s.startswith(p), "base_ref": base, "head_ref": head, "__builtins__": {}}
    # `source` holds only what _to_python() emits: names, string literals,
    # `==`, `and`, `or`, parentheses and `_starts(...)` calls.
    return bool(eval(compile(source, "<branch-guard>", "eval"), namespace))


class TestBranchGuard:
    @pytest.mark.parametrize(
        "head",
        [
            "v3",
            "v3/fix-something",
            # The v3 branch monitor's pin and release branches.
            "chore/surrealdb-v3-3.3.6",
            "chore/release-v3-v0.39.10",
            # v2 stays blocked.
            "v2",
            "v2/fix-something",
            "chore/surrealdb-2x-2.7.1",
        ],
    )
    def test_v2_and_v3_heads_are_refused_into_main(self, head: str) -> None:
        assert _guard_blocks("main", head), f"{head} could be merged into main"

    @pytest.mark.parametrize(
        "head",
        [
            "ci/579-v3-guard-monitor-sync",
            "fix/123-v3-something",
            "v30",
            "v3-notes",
            "chore/surrealdb-3.3.6",
            "chore/release-v0.39.10",
            "chore/version-bump-0.39.10",
        ],
    )
    def test_main_branches_are_not_refused(self, head: str) -> None:
        assert not _guard_blocks("main", head), f"{head} was refused into main"

    @pytest.mark.parametrize("head", ["v3/fix-something", "chore/surrealdb-v3-3.3.6", "chore/release-v3-v0.39.10"])
    def test_prs_into_v3_are_not_refused(self, head: str) -> None:
        assert not _guard_blocks("v3", head)

    def test_the_translator_refuses_an_unknown_shape(self) -> None:
        with pytest.raises(AssertionError, match="unsupported"):
            _to_python("contains(github.head_ref, 'v3')")

    def test_ci_success_names_both_lines(self) -> None:
        step = _load("ci.yml")["jobs"]["ci-success"]["steps"][0]
        assert "v2 or v3 branch cannot be merged into main" in step["run"]


class TestTagReleaseOnMain:
    def test_main_tags_from_main_only(self) -> None:
        """`v3` and `v2` each run their own `tag-release.yml`; main's names main alone."""
        assert _triggers(_load("tag-release.yml"))["push"]["branches"] == ["main"]
