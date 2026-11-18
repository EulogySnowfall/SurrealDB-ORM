"""#579: `main`'s side of the permanent `v3` branch.

`v3` carries the SurrealDB 3.x line once `main` moves to 4.x. On `main` that
means: the branch guard refuses `v3` heads as it refuses `v2` ones, and
`tag-release.yml` on `main` names `main` only (`v3` has its own copy, which is
the one GitHub runs for a push to `v3`).

#698: `v3` owns the 0.39.x patch series, so `main` publishes nothing in it.
`publish.yml` refuses a `v0.39.*` tag and `tag-release.yml` a `0.39.*` release
subject; their shell steps run here in a git sandbox whose `origin` is a local
bare repository.

The guard's condition is a GitHub expression. It is evaluated here by a small
translator for the subset it uses (`==`, `&&`, `||`, `startsWith()`, string
literals, `github.base_ref` / `github.head_ref`), which refuses anything else,
so an edit into another shape fails these tests instead of being misread.
"""

from __future__ import annotations

import os
import re
import subprocess
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


# ---------------------------------------------------------------------------
# #698: main publishes nothing in the 0.39.x series
# ---------------------------------------------------------------------------

_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@t"]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *_IDENTITY, *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _sandbox(tmp_path: Path) -> tuple[Path, str, str]:
    """A clone whose ``origin`` has ``main`` and a side branch off it.

    Returns ``(clone, sha on main, sha only on the side branch)``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README").write_text("main\n")
    _git(seed, "add", "README")
    _git(seed, "commit", "-q", "-m", "main")
    on_main = _git(seed, "rev-parse", "HEAD")
    _git(seed, "checkout", "-q", "-b", "side")
    (seed / "README").write_text("side\n")
    _git(seed, "commit", "-q", "-am", "side")
    off_main = _git(seed, "rev-parse", "HEAD")
    _git(seed, "push", "-q", str(origin), "main", "side")
    clone = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "-b", "side", str(origin), str(clone)], check=True, capture_output=True)
    return clone, on_main, off_main


def _check_step() -> dict[str, Any]:
    step: dict[str, Any] = next(s for s in _load("publish.yml")["jobs"]["check-main"]["steps"] if s.get("id") == "check")
    return step


def _run_publish_check(tmp_path: Path, *, tag: str, sha: str, repo: Path) -> tuple[int, dict[str, str], str]:
    step = _check_step()
    script = tmp_path / "check.sh"
    script.write_text(step["run"], encoding="utf-8")
    outputs = tmp_path / "outputs"
    outputs.write_text("")
    env = {**os.environ, "TAG": tag, "TAGGED_SHA": sha, "GITHUB_OUTPUT": str(outputs)}
    # GitHub runs a `run:` block with no `shell:` as `bash -e {0}`.
    result = subprocess.run(["bash", "-e", str(script)], cwd=repo, env=env, capture_output=True, text=True)
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line)
    return result.returncode, values, result.stdout + result.stderr


class TestPublishRefusesThe039Series:
    def test_tag_and_sha_reach_the_script_through_env(self) -> None:
        step = _check_step()
        assert step["env"] == {"TAG": "${{ github.ref_name }}", "TAGGED_SHA": "${{ github.sha }}"}
        assert "${{" not in step["run"]

    @pytest.mark.parametrize("tag", ["v0.39.0", "v0.39.9", "v0.39.10", "v0.39.123", "v0.39"])
    def test_a_v0_39_tag_on_main_is_refused(self, tmp_path: Path, tag: str) -> None:
        repo, on_main, _ = _sandbox(tmp_path)
        code, outputs, log = _run_publish_check(tmp_path, tag=tag, sha=on_main, repo=repo)
        assert code != 0, log
        assert "::error::" in log
        assert outputs == {}, log

    @pytest.mark.parametrize("tag", ["v0.40.0", "v0.40.12", "v0.41.0"])
    def test_a_later_tag_on_main_publishes(self, tmp_path: Path, tag: str) -> None:
        repo, on_main, _ = _sandbox(tmp_path)
        code, outputs, log = _run_publish_check(tmp_path, tag=tag, sha=on_main, repo=repo)
        assert code == 0, log
        assert outputs == {"on-main": "true"}, log

    def test_a_tag_off_main_is_still_not_published(self, tmp_path: Path) -> None:
        repo, _, off_main = _sandbox(tmp_path)
        code, outputs, log = _run_publish_check(tmp_path, tag="v0.40.0", sha=off_main, repo=repo)
        assert code == 0, log
        assert outputs == {"on-main": "false"}, log

    def test_nothing_is_built_published_or_released_without_the_check(self) -> None:
        jobs = _load("publish.yml")["jobs"]
        assert jobs["build"]["needs"] == "check-main"
        assert jobs["build"]["if"] == "needs.check-main.outputs.on-main == 'true'"
        assert jobs["publish-pypi"]["needs"] == "build"
        assert jobs["github-release"]["needs"] == ["check-main", "build"]
        assert jobs["github-release"]["if"] == "needs.check-main.outputs.on-main == 'true'"


def _run_tag_version(tmp_path: Path, message: str) -> tuple[int, dict[str, str], str]:
    step = next(s for s in _load("tag-release.yml")["jobs"]["tag-release"]["steps"] if s.get("id") == "version")
    assert "${{" not in step["run"]
    script = tmp_path / "version.sh"
    script.write_text(step["run"], encoding="utf-8")
    outputs = tmp_path / "outputs"
    outputs.write_text("")
    env = {**os.environ, "COMMIT_MSG": message, "REF_NAME": "main", "GITHUB_OUTPUT": str(outputs)}
    result = subprocess.run(["bash", "-e", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True)
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines() if "=" in line)
    return result.returncode, values, result.stdout + result.stderr


class TestTagReleaseRefusesThe039Series:
    @pytest.mark.parametrize("version", ["0.39.0", "0.39.9", "0.39.10", "0.39"])
    def test_a_0_39_release_subject_is_refused(self, tmp_path: Path, version: str) -> None:
        code, outputs, log = _run_tag_version(tmp_path, f"chore(release): bump version to {version} (#1)")
        assert code != 0, log
        assert "::error::" in log
        assert outputs == {}, log

    @pytest.mark.parametrize("version", ["0.40.0", "0.40.1", "1.0.0"])
    def test_a_later_release_subject_is_tagged(self, tmp_path: Path, version: str) -> None:
        code, outputs, log = _run_tag_version(tmp_path, f"chore(release): bump version to {version} (#1)")
        assert code == 0, log
        assert outputs["tag"] == f"v{version}", log
