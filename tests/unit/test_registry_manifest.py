"""server.json names a version, and it has to be the one being released.

The release workflow checks this manifest against the tag, which is correct
and is also too late: that check runs in the publish job, **after** PyPI has
already accepted the package. On v0.5.7 it fired exactly as designed and the
result was a version published to PyPI and absent from the MCP registry, with
the only recovery being a separate workflow built after the same thing
happened to v0.5.6.

A test is the right place for it. It costs nothing, runs on the pull request
that bumps the version, and fails before anything is published rather than
between two things that were.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def manifest() -> dict:
    return json.loads((ROOT / "server.json").read_text())


def package_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def test_the_manifest_version_is_the_package_version():
    """The failure this exists for. A manifest left at the previous version
    does not error: it re-publishes the OLD version to the registry, so the
    registry quietly points at a release that is no longer current."""
    assert manifest()["version"] == package_version()


def test_every_version_in_the_manifest_agrees():
    """It appears twice - once for the server and once for the package entry -
    and they are edited by hand. Two copies of a number are two chances to
    change one of them."""
    found = {manifest()["version"]}
    for package in manifest().get("packages", []):
        found.add(package["version"])

    assert found == {package_version()}, (
        f"server.json carries versions {sorted(found)}, pyproject says "
        f"{package_version()}"
    )


def test_the_manifest_points_at_the_package_that_is_published():
    """The registry entry is only useful if the thing it names is installable.
    The distribution is `echo-mem`, not `echo-memory`, which belongs to an
    unrelated product."""
    packages = manifest().get("packages", [])

    assert packages, "server.json lists no package"
    pypi = [p for p in packages if p.get("registryType") == "pypi"]
    assert pypi, "no PyPI package in server.json"
    assert pypi[0]["identifier"] == "echo-mem"
