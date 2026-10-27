"""
Unit tests for #245 on the v2 (SurrealDB 2.x) line: the declared aiohttp floor.

The lock has resolved aiohttp 3.14.5 since 0.21.10, but downstream installs never
read ``uv.lock``: they honour only the requirement declared in the package
metadata. With ``aiohttp>=3.14.0`` declared, ``pip install -U surrealdb-orm``
(``only-if-needed``) and existing downstream lockfiles kept an installed
3.14.0 - 3.14.3, which every advisory fixed in 3.14.4 affects. The 0.21.10 notes
asked users to upgrade aiohttp by hand instead.

The SDK is bundled with its own ``pyproject.toml``, so the floor is declared
twice and both must move together.
"""

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
PYPROJECTS = ["pyproject.toml", "src/surreal_sdk/pyproject.toml"]

# Last release affected by GHSA-494j-54xm-rwp3, GHSA-2g87-pp6c-29x5,
# GHSA-5wr8-gcqf-gqf3 and the other advisories first patched in 3.14.4.
LAST_VULNERABLE_AIOHTTP = "3.14.3"
FIRST_PATCHED_AIOHTTP = "3.14.4"


def _requirement(pyproject: str, name: str) -> Requirement:
    """Return the runtime requirement on ``name`` declared in ``pyproject``."""
    deps = tomllib.loads((ROOT / pyproject).read_text())["project"]["dependencies"]
    matches = [Requirement(dep) for dep in deps if Requirement(dep).name == name]
    assert len(matches) == 1, f"{pyproject}: expected one {name} requirement, found {matches}"
    return matches[0]


@pytest.mark.parametrize("pyproject", PYPROJECTS)
def test_aiohttp_floor_excludes_releases_before_3_14_4(pyproject: str) -> None:
    specifier = _requirement(pyproject, "aiohttp").specifier
    assert not specifier.contains(LAST_VULNERABLE_AIOHTTP), (
        f"{pyproject}: aiohttp {specifier} still admits {LAST_VULNERABLE_AIOHTTP}, "
        "so upgrading surrealdb-orm can keep a vulnerable aiohttp installed"
    )
    assert specifier.contains(FIRST_PATCHED_AIOHTTP), f"{pyproject}: aiohttp {specifier} rejects the patched release"


def test_bundled_sdk_declares_the_same_aiohttp_requirement() -> None:
    package, sdk = (_requirement(path, "aiohttp") for path in PYPROJECTS)
    assert str(package) == str(sdk), f"package declares {package}, bundled SDK declares {sdk}"


@pytest.mark.parametrize("pyproject", PYPROJECTS)
def test_cbor2_stays_below_6_on_v2(pyproject: str) -> None:
    """cbor2 6.x breaks the SurrealDB 2.x custom-tag encoding this line relies on."""
    specifier = _requirement(pyproject, "cbor2").specifier
    assert not specifier.contains("6.0.0"), f"{pyproject}: cbor2 {specifier} admits 6.x on the v2 line"
