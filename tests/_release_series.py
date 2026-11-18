"""Git sandboxes for the release-automation tests of #579 and #698.

The ``v3`` branch owns the 0.39.x patch series (#698): ``main`` releases
nothing below 0.40.0, and ``v3``'s automation runs whatever ``main``'s version
is. The version bump steps read the branch's ``pyproject.toml`` and ask
``origin`` whether a tag already exists, and the sync and monitor jobs check out
a branch whose ``origin`` also carries ``main``, so the tests run those steps
in a clone whose ``origin`` is a local bare repository.

``run_job()`` runs every ``run:`` step of a job in order, the way GitHub does
(``bash -e``), skipping the steps whose ``if:`` is false, so a test sees what
the whole job does rather than one step taken alone.

Not a test module (no ``test_`` prefix): ``test_sync_v2_grouped.py`` and
``test_surrealdb_monitor.py`` import it.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@t"]


def pyproject(version: str) -> str:
    """A minimal ``pyproject.toml`` declaring ``version``."""
    return f'[project]\nname = "surrealdb-orm"\nversion = "{version}"\n'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *_IDENTITY, *args], check=True, capture_output=True)


def _commit(repo: Path, files: dict[str, str], msg: str) -> None:
    (repo / "README").write_text(msg)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "README", *files)
    _git(repo, "commit", "-q", "-m", msg)


def owner_repo(tmp_path: Path, *, main: str | None, target_version: str, target: str) -> Path:
    """A clone of ``target`` whose ``origin`` also has ``main`` (unless ``main`` is None).

    ``main`` / ``target_version`` are the ``pyproject.toml`` contents committed
    on that branch; an empty string commits no ``pyproject.toml``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)

    if main is not None:
        _commit(seed, {"pyproject.toml": main} if main else {}, "main")
        _git(seed, "push", "-q", str(origin), "main")
        _git(seed, "checkout", "-q", "--orphan", target)
        _git(seed, "rm", "-rq", "--cached", "--ignore-unmatch", ".")
        (seed / "pyproject.toml").unlink(missing_ok=True)
    else:
        _git(seed, "symbolic-ref", "HEAD", f"refs/heads/{target}")
    _commit(seed, {"pyproject.toml": target_version} if target_version else {}, target)
    _git(seed, "push", "-q", str(origin), target)
    subprocess.run(["git", "clone", "-q", "-b", target, str(origin), str(repo)], check=True, capture_output=True)
    return repo


def bump_repo(
    tmp_path: Path,
    *,
    branch: str,
    version: str,
    tags: list[str],
    pyproject_text: str | None = None,
    main_version: str | None = None,
) -> tuple[Path, Path]:
    """A clone of ``branch`` carrying the four version files at ``version``.

    ``origin`` also carries each of ``tags`` (lightweight tags on the branch
    head). ``pyproject_text`` replaces the root ``pyproject.toml``;
    ``main_version`` also pushes a ``main`` branch at that version (for a
    ``branch`` other than ``main``). Returns ``(clone, origin)``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    if main_version is not None:
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
        _commit(seed, {"pyproject.toml": pyproject(main_version)}, "main")
        _git(seed, "push", "-q", str(origin), "main")
        _git(seed, "checkout", "-q", "--orphan", branch)
        _git(seed, "rm", "-rq", "--cached", "--ignore-unmatch", ".")
    else:
        subprocess.run(["git", "init", "-q", "-b", branch, str(seed)], check=True)
    files = {
        "pyproject.toml": pyproject(version) if pyproject_text is None else pyproject_text,
        "src/surreal_sdk/pyproject.toml": f'[project]\nname = "surreal-sdk"\nversion = "{version}"\n',
        "src/surreal_orm/__init__.py": f'__version__ = "{version}"\n',
        "src/surreal_sdk/__init__.py": f'__version__ = "{version}"\n',
        "uv.lock": "version = 1\n",
    }
    _commit(seed, files, branch)
    for tag in tags:
        _git(seed, "tag", tag)
    _git(seed, "push", "-q", str(origin), branch, *tags)
    subprocess.run(["git", "clone", "-q", "-b", branch, str(origin), str(repo)], check=True, capture_output=True)
    return repo, origin


def remote_refs(origin: Path) -> list[str]:
    """Every ref of a bare repository (branches and tags)."""
    out = subprocess.run(
        ["git", "-C", str(origin), "for-each-ref", "--format=%(refname)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.split()


# ---------------------------------------------------------------------------
# Running a whole job
# ---------------------------------------------------------------------------

_EXPR = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_STEP_OUTPUT = re.compile(r"steps\.([\w-]+)\.outputs\.([\w-]+)")
_CONDITION = re.compile(r"steps\.([\w-]+)\.outputs\.([\w-]+) == '([^']*)'")


@dataclass
class JobRun:
    """What ``run_job()`` saw: exit status, each step's outputs by id, the steps run."""

    returncode: int
    outputs: dict[str, dict[str, str]] = field(default_factory=dict)
    ran: list[str] = field(default_factory=list)
    log: str = ""


def _enabled(condition: object, outputs: Mapping[str, Mapping[str, str]]) -> bool:
    """Evaluate a step ``if:`` made of ``steps.<id>.outputs.<key> == '<value>'`` terms joined by ``&&``.

    Any other shape is refused, so an edit into a condition this reader does
    not understand fails the tests instead of being misread.
    """
    if condition is None:
        return True
    for term in " ".join(str(condition).split()).split("&&"):
        match = _CONDITION.fullmatch(term.strip())
        if match is None:
            raise AssertionError(f"unsupported step condition: {condition!r}")
        step, key, want = match.groups()
        if outputs.get(step, {}).get(key) != want:
            return False
    return True


def _read_outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def run_job(
    steps: list[dict[str, Any]],
    *,
    cwd: Path,
    scratch: Path,
    env: Mapping[str, str],
    context: Mapping[str, str],
) -> JobRun:
    """Run a job's ``run:`` steps in order in ``cwd``; stop at the first failure.

    ``uses:`` steps are not run: the sandbox stands for the checkout and the
    tool set-up. A step's ``env:`` expressions are expanded from the earlier
    steps' outputs (``steps.<id>.outputs.<key>``) and from ``context`` (every
    other expression); an expression in neither is refused. A ``run:`` block
    must not contain an expression: GitHub would expand it into the script.
    """
    run = JobRun(returncode=0)

    def expand(value: str) -> str:
        def sub(match: re.Match[str]) -> str:
            expr = match.group(1)
            ref = _STEP_OUTPUT.fullmatch(expr)
            if ref is not None:
                return run.outputs.get(ref.group(1), {}).get(ref.group(2), "")
            if expr in context:
                return context[expr]
            raise AssertionError(f"unexpected expression: ${{{{ {expr} }}}}")

        return _EXPR.sub(sub, value)

    scratch.mkdir(parents=True, exist_ok=True)
    for index, step in enumerate(steps):
        if "run" not in step or not _enabled(step.get("if"), run.outputs):
            continue
        script_text = str(step["run"])
        assert "${{" not in script_text, f"{step.get('name')!r} expands an expression into its script"
        script = scratch / f"step-{index}.sh"
        script.write_text(script_text, encoding="utf-8")
        output = scratch / f"step-{index}.out"
        output.write_text("", encoding="utf-8")
        step_env = {**env, "GITHUB_OUTPUT": str(output)}
        step_env.update({key: expand(str(value)) for key, value in (step.get("env") or {}).items()})
        # GitHub runs a `run:` block with no `shell:` as `bash -e {0}`.
        result = subprocess.run(["bash", "-e", str(script)], cwd=cwd, env=step_env, capture_output=True, text=True)
        run.ran.append(str(step.get("name")))
        run.log += result.stdout + result.stderr
        if "id" in step:
            run.outputs[str(step["id"])] = _read_outputs(output)
        if result.returncode != 0:
            run.returncode = result.returncode
            break
    return run
