"""The licence this project claims has to be the one in the file.

A package that says Apache-2.0 in its metadata while shipping a Business
Source License is making a false statement to everybody who installs it, and
those are exactly the people who check metadata rather than read a LICENSE.

These pin the three places the claim appears and the one fact that no licence
change can alter: versions already published stay under the licence they went
out with.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def flat(path: Path) -> str:
    """The file with its line wrapping collapsed.

    A licence is wrapped for a human to read, so asserting on raw text makes
    a test fail when a phrase happens to straddle a newline - which says
    nothing about whether the licence is right. This one did exactly that on
    "fewer than 50 employees".
    """
    return " ".join(path.read_text().split())


def test_the_licence_file_is_the_business_source_license():
    text = flat(ROOT / "LICENSE")

    assert "Business Source License 1.1" in text
    assert "Licensor: Ateleir Tech" in text
    assert "Change License: Apache License, Version 2.0" in text, (
        "a BSL with no change licence never becomes open source"
    )


def test_the_additional_use_grant_says_who_may_use_it_free():
    """A BSL with no grant, or a vague one, is a licence nobody can act on
    without asking a lawyer - which for a small team means not adopting it."""
    text = flat(ROOT / "LICENSE")

    assert "fewer than 50 employees" in text
    assert "US$5,000,000" in text
    assert "non-production purpose" in text
    assert "parent, subsidiary or affiliated entity" in text, (
        "without this, a subsidiary of a large company reads as a small one"
    )


def test_the_package_metadata_does_not_still_claim_apache():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    licence = data["project"]["license"]

    assert licence == {"file": "LICENSE"}, (
        f"pyproject says {licence!r}, which is not what LICENSE contains"
    )
    classifiers = " ".join(data["project"].get("classifiers", []))
    assert "Apache" not in classifiers, (
        "a classifier still advertises Apache-2.0"
    )


def test_the_apache_text_is_kept_for_the_versions_released_under_it():
    """Relicensing cannot reach backwards. Everything up to 0.4.1 went out
    under Apache 2.0 and stays there, so the text those users are entitled to
    has to remain in the repository."""
    apache = ROOT / "LICENSE-APACHE-2.0"

    assert apache.exists(), "the licence earlier releases were made under is gone"
    assert "Apache License" in apache.read_text()


def test_the_readme_states_that_earlier_versions_stay_apache():
    """Somebody on 0.4.1 needs to know where they stand without reading a git
    history or a lawyer."""
    readme = flat(ROOT / "README.md")

    assert "remain under Apache 2.0" in readme
    assert "0.4.1" in readme
