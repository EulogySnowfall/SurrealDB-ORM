"""Guards for the declared runtime dependency floors (#291).

``uv.lock`` only pins what the project's own CI installs. A downstream
``pip install -U surrealdb-orm`` (default ``only-if-needed`` strategy) and an
existing downstream lock file honour the floors declared in ``pyproject.toml``
instead, so a fix that reaches only ``uv.lock`` never reaches installed users.

These tests keep the declared floors honest:

* the package and the bundled SDK declare the same specifier for every runtime
  dependency they share, so installing either one gives the same guarantee;
* the ``aiohttp`` floor excludes every release affected by GHSA-494j-54xm-rwp3,
  GHSA-2g87-pp6c-29x5 and GHSA-5wr8-gcqf-gqf3 (all ``<= 3.14.3``);
* the ``aiohttp`` version locked for CI satisfies that floor.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parent.parent
ORM_PYPROJECT = ROOT / "pyproject.toml"
SDK_PYPROJECT = ROOT / "src" / "surreal_sdk" / "pyproject.toml"
LOCK_FILE = ROOT / "uv.lock"

# Last aiohttp release affected by the advisories the floor covers.
AIOHTTP_LAST_AFFECTED = Version("3.14.3")
AIOHTTP_FIRST_FIXED = Version("3.14.4")


def _runtime_requirements(pyproject: Path) -> dict[str, Requirement]:
    """Return the ``[project] dependencies`` of a pyproject, by canonical name."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    requirements = [Requirement(spec) for spec in data["project"]["dependencies"]]
    return {canonicalize_name(req.name): req for req in requirements}


def _locked_version(package: str) -> Version:
    """Return the version ``uv.lock`` resolves for ``package``."""
    lock = tomllib.loads(LOCK_FILE.read_text(encoding="utf-8"))
    versions = [
        Version(entry["version"]) for entry in lock["package"] if canonicalize_name(entry["name"]) == canonicalize_name(package)
    ]
    assert len(versions) == 1, f"expected one locked {package}, found {versions}"
    return versions[0]


class TestI291DependencyFloors:
    """The declared floors, not only the lock, carry the security fixes."""

    def test_orm_and_sdk_declare_the_same_shared_floors(self) -> None:
        orm = _runtime_requirements(ORM_PYPROJECT)
        sdk = _runtime_requirements(SDK_PYPROJECT)
        shared = sorted(orm.keys() & sdk.keys())
        assert "aiohttp" in shared
        mismatched = {
            name: (str(orm[name].specifier), str(sdk[name].specifier))
            for name in shared
            if orm[name].specifier != sdk[name].specifier
        }
        assert mismatched == {}

    def test_aiohttp_floor_excludes_the_affected_releases(self) -> None:
        for pyproject in (ORM_PYPROJECT, SDK_PYPROJECT):
            specifier = _runtime_requirements(pyproject)["aiohttp"].specifier
            assert not specifier.contains(AIOHTTP_LAST_AFFECTED), (
                f"{pyproject.relative_to(ROOT)}: aiohttp{specifier} still accepts {AIOHTTP_LAST_AFFECTED}"
            )
            assert specifier.contains(AIOHTTP_FIRST_FIXED), (
                f"{pyproject.relative_to(ROOT)}: aiohttp{specifier} rejects {AIOHTTP_FIRST_FIXED}"
            )

    def test_locked_aiohttp_satisfies_the_declared_floor(self) -> None:
        locked = _locked_version("aiohttp")
        for pyproject in (ORM_PYPROJECT, SDK_PYPROJECT):
            specifier = _runtime_requirements(pyproject)["aiohttp"].specifier
            assert specifier.contains(locked), f"{pyproject.relative_to(ROOT)}: locked aiohttp {locked} outside {specifier}"
