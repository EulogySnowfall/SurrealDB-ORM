"""Git sandboxes for the release-automation tests of #579.

One patch series has one owner: while ``main``'s ``pyproject.toml`` has the
same X.Y as ``v3``'s, ``main`` owns that series and ``v3``'s automation does
nothing. The workflow steps that decide this read both versions through git,
and the version bump steps ask ``origin`` whether a tag already exists, so the
tests run those steps in a clone whose ``origin`` is a local bare repository.

Not a test module (no ``test_`` prefix): ``test_sync_v2_grouped.py`` and
``test_surrealdb_monitor.py`` import it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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


def bump_repo(tmp_path: Path, *, branch: str, version: str, tags: list[str]) -> tuple[Path, Path]:
    """A clone of ``branch`` carrying the four version files at ``version``.

    ``origin`` also carries each of ``tags`` (lightweight tags on the branch
    head). Returns ``(clone, origin)``.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(seed)], check=True)
    files = {
        "pyproject.toml": pyproject(version),
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
